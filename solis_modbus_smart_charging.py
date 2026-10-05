"""
Solis Modbus Smart Charging for Home Assistant (PyScript)
Version: 1.1.1

Syncs Solis inverter charge slots with Octopus Energy Intelligent dispatch
periods, writing the times locally through the Solis Modbus integration
(https://github.com/Pho3niX90/solis_modbus).

Changes in 1.1.1:
- Removed an unsupported keyword from the time.set_value service call that
  Home Assistant rejected ("extra keys not allowed").
- entity_prefix is normalised, so "solis", "time.solis" and "sensor.solis"
  all resolve to the correct time entities.
- Slot 1 is checked before any writes, with a clear error if it is missing.
- Unavailable entities are now treated as missing, so the script no longer
  reports success when the Solis Modbus integration is not polling.
- Writes and failures are counted and reported in the result.

Changes in 1.1.0:
- Writes go through the time.set_value service instead of the private
  hass.data["solis_modbus"]["time_entities"] structure, which broke in
  solis_modbus 4.2.x.
"""

VERSION = "1.1.1"

import json
from datetime import datetime, timedelta, timezone
import logging
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import hashlib
import hmac
import base64
import re
from http import HTTPStatus

log = logging.getLogger("pyscript.solis_modbus_smart_charging")
log.setLevel(logging.DEBUG)

def debug_log(prefix, message):
    log.debug(f"{prefix}: {message}")

# Constants
VERB = "POST"
LOGIN_URL = '/v2/api/login'
CONTROL_URL= '/v2/api/control'
INVERTER_URL= '/v1/api/inverterList'

# API Helper Functions - Keeping exactly as is
def digest(body: str) -> str:
    return base64.b64encode(hashlib.md5(body.encode('utf-8')).digest()).decode('utf-8')

def passwordEncode(password: str) -> str:
    return hashlib.md5(password.encode('utf-8')).hexdigest()

def prepare_header(config: dict[str,str], body: str, canonicalized_resource: str) -> dict[str, str]:
    content_md5 = digest(body)
    content_type = "application/json"
    now = datetime.now(timezone.utc)
    date = now.strftime("%a, %d %b %Y %H:%M:%S GMT")
    encrypt_str = (VERB + "\n" + content_md5 + "\n" + content_type + "\n" + date + "\n" + canonicalized_resource)
    hmac_obj = hmac.new(
        config["secret"].encode('utf-8'),
        msg=encrypt_str.encode('utf-8'),
        digestmod=hashlib.sha1
    )
    sign = base64.b64encode(hmac_obj.digest())
    authorization = "API " + str(config["key_id"]) + ":" + sign.decode('utf-8')
    header = {
        "Content-MD5": content_md5,
        "Content-Type": content_type,
        "Date": date,
        "Authorization": authorization
    }
    return header

def control_body(inverterId, chargeSettings) -> str:
    body = '{"inverterId":"'+str(inverterId)+'", "cid":"103","value":"'
    for index, time in enumerate(chargeSettings):
        body = body + str(time['chargeCurrent'])+","+str(time['dischargeCurrent'])+","+str(time['chargeStartTime'])+","+str(time['chargeEndTime'])+","+str(time['dischargeStartTime'])+","+str(time['dischargeEndTime'])
        if (index != 2):
            body = body+","
    return body+'"}'


def resolve_slot_entities(entity_prefix, slot):
    """Build the time entity IDs for a charge slot.

    Accepts a prefix with or without a domain. The Solis Modbus integration
    exposes these as `time.` entities, so anything else is normalised:
        "solis"              -> time.solis_time_charging_charge_start_slot_1
        "time.solis"         -> time.solis_time_charging_charge_start_slot_1
        "sensor.solis"       -> time.solis_time_charging_charge_start_slot_1
    """
    prefix = str(entity_prefix).strip()

    if "." in prefix:
        base = prefix.split(".", 1)[1]
    else:
        base = prefix

    start_id = f"time.{base}_time_charging_charge_start_slot_{slot}"
    end_id = f"time.{base}_time_charging_charge_end_slot_{slot}"
    return start_id, end_id


def entity_exists(entity_id):
    """Return True if the entity is present and available.

    An unavailable entity is treated as missing: a blocking time.set_value
    call against one only logs a warning and writes nothing, so counting it
    as written would report success when the inverter was never updated.
    This is what happens when the Solis Modbus integration stops polling.
    """
    try:
        current = state.get(entity_id)
    except Exception:
        return False
    if current is None:
        return False
    return str(current).lower() != "unavailable"


class WindowProcessor:
    def __init__(self):
        self.core_window = None
        self.dispatch_blocks = []
        self.charging_windows = []

    def initialize_core_window(self, first_dispatch_time):
        """Initialize core window based on first dispatch timezone and date.
        If dispatches are received during early hours (00:00-12:00), align core window to previous day."""
        dispatch_tz = first_dispatch_time.tzinfo
        dispatch_hour = first_dispatch_time.hour

        # If we receive dispatches between midnight and noon,
        # we're probably processing the current night's schedule
        if 0 <= dispatch_hour < 12:
            dispatch_date = (first_dispatch_time - timedelta(days=1)).date()
        else:
            dispatch_date = first_dispatch_time.date()

        next_date = dispatch_date + timedelta(days=1)

        # Initialize core window with same timezone as dispatches
        core_start = datetime.combine(
            dispatch_date,
            datetime.strptime('23:30', '%H:%M').time()
        ).replace(tzinfo=dispatch_tz)
        core_end = datetime.combine(
            next_date,
            datetime.strptime('05:30', '%H:%M').time()
        ).replace(tzinfo=dispatch_tz)

        self.core_window = {
            'start': core_start,
            'end': core_end
        }
        log.debug(f"Initialized core window: {self.core_window['start']} to {self.core_window['end']}")

    def round_to_slot(self, dt: datetime, is_end_time: bool = False) -> datetime:
        """Round datetime to nearest 30-minute slot."""
        result = dt.replace(second=0, microsecond=0)
        minute = result.minute

        if is_end_time:
            if minute > 0:
                if minute <= 30:
                    result = result.replace(minute=30)
                else:
                    result = result + timedelta(hours=1)
                    result = result.replace(minute=0)
        else:
            result = result.replace(minute=(minute // 30) * 30)

        return result

    def normalize_dispatch(self, dispatch: dict) -> dict:
        """Normalize a dispatch window, maintaining all original attributes."""
        normalized = {
            'start': self.round_to_slot(dispatch['start'], False),
            'end': self.round_to_slot(dispatch['end'], True),
            'duration_minutes': (dispatch['end'] - dispatch['start']).total_seconds() / 60
        }

        # Copy any additional attributes
        for k, v in dispatch.items():
            if k not in ['start', 'end']:
                normalized[k] = v

        return normalized

    def normalize_dispatches(self, dispatches: list) -> list:
        """Process incoming dispatch windows."""
        log.debug(f"\nProcessing {len(dispatches)} dispatch windows")

        if not dispatches:
            return []

        # Initialize core window based on first dispatch
        if self.core_window is None:
            self.initialize_core_window(dispatches[0]['start'])

        self.dispatch_blocks = []
        valid_dispatches = []

        # First pass: normalize all windows
        for dispatch in dispatches:
            normalized = self.normalize_dispatch(dispatch)
            log.debug(f"Normalized window: {normalized['start']} to {normalized['end']}")
            valid_dispatches.append(normalized)

        # Bubble sort by start time
        n = len(valid_dispatches)
        for i in range(n):
            for j in range(0, n - i - 1):
                if valid_dispatches[j]['start'] > valid_dispatches[j + 1]['start']:
                    valid_dispatches[j], valid_dispatches[j + 1] = valid_dispatches[j + 1], valid_dispatches[j]

        log.debug("Sorted windows:")
        for window in valid_dispatches:
            log.debug(f"  {window['start']} to {window['end']}")

        # Merge contiguous windows
        if valid_dispatches:
            current_window = valid_dispatches[0].copy()

            for next_window in valid_dispatches[1:]:
                # Check for contiguous or overlapping windows
                if (next_window['start'] - current_window['end']).total_seconds() <= 1:
                    log.debug(f"Merging windows: {current_window['end']} and {next_window['start']}")
                    # Extend current window
                    current_window['end'] = max(current_window['end'], next_window['end'])
                    current_window['duration_minutes'] = (current_window['end'] - current_window['start']).total_seconds() / 60
                else:
                    self.dispatch_blocks.append(current_window)
                    current_window = next_window.copy()

            self.dispatch_blocks.append(current_window)

        log.debug("\nFinal merged dispatch blocks:")
        for block in self.dispatch_blocks:
            log.debug(f"  {block['start']} to {block['end']} (duration: {block['duration_minutes']} mins)")

        return self.dispatch_blocks

    def process_core_hours(self):
        """Process windows against core hours and extend if needed."""
        if not self.core_window:
            return

        log.debug(f"\nProcessing core hours")
        log.debug(f"Initial core window: {self.core_window['start']} to {self.core_window['end']}")

        while True:
            changes_made = False
            remaining_blocks = []

            for window in self.dispatch_blocks:
                log.debug(f"\nChecking window: {window['start']} to {window['end']}")

                # Check if window overlaps core
                if (window['start'] <= self.core_window['end'] and
                        window['end'] >= self.core_window['start']):

                    if window['start'] < self.core_window['start']:
                        log.debug(f"Extending core start from {self.core_window['start']} to {window['start']}")
                        self.core_window['start'] = window['start']
                        changes_made = True

                    if window['end'] > self.core_window['end']:
                        log.debug(f"Extending core end from {self.core_window['end']} to {window['end']}")
                        self.core_window['end'] = window['end']
                        changes_made = True
                else:
                    log.debug("Window outside core - keeping for additional windows")
                    remaining_blocks.append(window)

            self.dispatch_blocks = remaining_blocks

            if not changes_made:
                break

        log.debug(f"\nAfter core processing:")
        log.debug(f"Final core window: {self.core_window['start']} to {self.core_window['end']}")

        if remaining_blocks:
            log.debug("Remaining windows for additional selection:")
            for block in remaining_blocks:
                log.debug(f"  {block['start']} to {block['end']} (duration: {block['duration_minutes']} mins)")
        else:
            log.debug("No remaining windows for additional selection")

    def select_additional_windows(self):
        """Select up to two additional windows based on duration."""
        if not self.dispatch_blocks:
            return []

        log.debug("\nSelecting additional windows")

        # Bubble sort by duration (longest first)
        blocks = self.dispatch_blocks.copy()
        n = len(blocks)
        for i in range(n):
            for j in range(0, n - i - 1):
                if blocks[j]['duration_minutes'] < blocks[j + 1]['duration_minutes']:
                    blocks[j], blocks[j + 1] = blocks[j + 1], blocks[j]

        selected = blocks[:2]

        log.debug("Selected windows:")
        for window in selected:
            log.debug(f"  {window['start']} to {window['end']} (duration: {window['duration_minutes']} mins)")

        return selected

    def format_charging_windows(self, additional_windows):
        """Format windows for the inverter."""
        log.debug("\nFormatting charging windows")

        if not self.core_window:
            # Initialize with default core window using current time
            self.initialize_core_window(datetime.now(timezone.utc))

        # Add core window
        self.charging_windows = [{
            "chargeCurrent": "60",
            "dischargeCurrent": "100",
            "chargeStartTime": self.core_window['start'].strftime("%H:%M"),
            "chargeEndTime": self.core_window['end'].strftime("%H:%M"),
            "dischargeStartTime": "00:00",
            "dischargeEndTime": "00:00"
        }]
        log.debug(f"Core window: {self.charging_windows[0]}")

        # Add additional windows
        for window in additional_windows:
            formatted = {
                "chargeCurrent": "60",
                "dischargeCurrent": "100",
                "chargeStartTime": window['start'].strftime("%H:%M"),
                "chargeEndTime": window['end'].strftime("%H:%M"),
                "dischargeStartTime": "00:00",
                "dischargeEndTime": "00:00"
            }
            self.charging_windows.append(formatted)
            log.debug(f"Additional window: {formatted}")

        # Fill with dummy windows if needed
        while len(self.charging_windows) < 3:
            dummy = {
                "chargeCurrent": "60",
                "dischargeCurrent": "100",
                "chargeStartTime": "00:00",
                "chargeEndTime": "00:00",
                "dischargeStartTime": "00:00",
                "dischargeEndTime": "00:00"
            }
            self.charging_windows.append(dummy)
            log.debug(f"Added dummy window: {dummy}")

        return self.charging_windows


@service
def solis_modbus_smart_charging(config=None):
    """
    PyScript service to sync Solis charging windows with Octopus dispatch periods via HA Modbus.
    """
    log.info(f"=== Solis Modbus Smart Charging v{VERSION} ===")

    if not config:
        log.error("No configuration provided")
        return "No configuration provided"

    if isinstance(config, str):
        try:
            config = task.executor(json.loads, config)
        except json.JSONDecodeError as e:
            log.error(f"Invalid JSON configuration: {str(e)}")
            return "Invalid JSON configuration"

    required_keys = ['entity_prefix']
    missing_keys = [key for key in required_keys if key not in config]
    if missing_keys:
        msg = f"Missing required configuration keys: {', '.join(missing_keys)}"
        log.error(msg)
        return msg

    # Process dispatch windows using existing WindowProcessor
    processor = WindowProcessor()
    dispatch_sensor = config.get('dispatch_sensor')

    try:
        dispatches = state.getattr(dispatch_sensor)
        if dispatches and 'planned_dispatches' in dispatches:
            processor.normalize_dispatches(dispatches['planned_dispatches'])
            processor.process_core_hours()
            additional_windows = processor.select_additional_windows()
            charging_windows = processor.format_charging_windows(additional_windows)
        else:
            log.warning(f"No dispatch data found for sensor {dispatch_sensor}")
            charging_windows = processor.format_charging_windows([])
    except Exception as e:
        log.error(f"Error processing dispatch windows: {str(e)}")
        charging_windows = processor.format_charging_windows([])

    # Write each charge slot via the time.set_value service.
    #
    # Earlier versions grabbed entity objects out of
    # hass.data["solis_modbus"]["time_entities"] and called async_set_value on
    # them directly. That is a private structure and its layout changed in
    # solis_modbus 4.2.x, which broke every write. Going through the service
    # gets us the same Modbus write via a supported interface.
    try:
        entity_prefix = config['entity_prefix']

        # Verify at least slot 1 resolves to a real entity before writing
        probe_start, probe_end = resolve_slot_entities(entity_prefix, 1)
        if not entity_exists(probe_start):
            msg = (
                f"Entity {probe_start} not found or unavailable. If it is missing, "
                f"check entity_prefix in your automation config - it should match the Solis Modbus device "
                f"naming, e.g. 'solis' for time.solis_time_charging_charge_start_slot_1. "
                f"Look under Developer Tools > States and filter on 'time.solis'. "
                f"If it exists but is unavailable, the Solis Modbus integration is not "
                f"polling the inverter - check its connection and logs."
            )
            log.error(msg)
            return msg

        written = 0
        failed = 0

        for slot, window in enumerate(charging_windows, 1):
            entity_id_start, entity_id_end = resolve_slot_entities(entity_prefix, slot)

            targets = [
                (entity_id_start, window['chargeStartTime']),
                (entity_id_end, window['chargeEndTime']),
            ]

            for entity_id, hhmm in targets:
                if not entity_exists(entity_id):
                    log.warning(f"Slot {slot}: entity {entity_id} not found or unavailable - skipping")
                    failed = failed + 1
                    continue

                try:
                    log.debug(f"Setting {entity_id} to {hhmm}")
                    service.call(
                        "time", "set_value",
                        blocking=True,
                        entity_id=entity_id,
                        time=f"{hhmm}:00"
                    )
                    written = written + 1
                except Exception as e:
                    log.error(f"Error setting time for {entity_id}: {str(e)}")
                    # Continue with next entity rather than failing completely
                    failed = failed + 1
                    continue

        if failed and not written:
            msg = f"Failed to write any charge slots ({failed} errors)"
            log.error(msg)
            return msg

        if failed:
            msg = f"Updated charging schedule with {written} writes, {failed} failed"
            log.warning(msg)
            return msg

        log.info(f"Successfully updated charging schedule ({written} slot times written)")
        return "Successfully updated charging schedule"

    except Exception as e:
        error_msg = f"Error updating schedule: {str(e)}"
        log.error(error_msg)
        return error_msg  # Return error message instead of raising exception