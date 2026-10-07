"""
Solis Modbus Smart Charging for Home Assistant (PyScript)
Version: 2.0.0

Syncs Solis inverter charge slots with Octopus Energy Intelligent dispatch
periods, writing the times locally through the Solis Modbus integration
(https://github.com/Pho3niX90/solis_modbus).

Two inverter firmware layouts are supported:

- legacy:   3 charge slots in the "Time-Charging" block (registers from 43143).
            Entities: time.<prefix>_time_charging_charge_start_slot_N
- six_slot: 6 charge slots in the "Grid Time of Use" (V2) block (registers
            from 43711), each with its own enable switch.
            Entities: time.<prefix>_grid_time_of_use_charge_start_slot_N
                      switch.<prefix>_grid_time_of_use_charging_period_N

The mode is detected automatically from the inverter's HMI version (0x4B or
above means six-slot firmware, the same threshold as the SolisCloud version of
this script) and confirmed by checking the Grid Time of Use entities are
available. It can be forced with "force_mode".

Inverter clock: the Solis Modbus integration corrects clock drift itself, so
this script does not set the inverter time.

Changes in 2.0.0:
- Six-slot (Grid Time of Use) firmware support, with automatic detection and
  a force_mode override (auto | legacy | six_slot).
- Six-slot mode switches on the enable switch for every slot it gives a
  charging window. Slot switches are never switched off.
- Only entities whose current value differs from the target are written.
- diagnostics_only mode: works out and reports the writes without making them.
- Optional per-slot charge current and charge cut-off SOC
  (set_charge_current / charge_current, set_charge_soc / charge_soc).
- Publishes sensor.solis_modbus_charge_schedule with the schedule, mode, and
  write results.
- dispatch_sensor is now required and is checked before anything is written.
- Removed unused SolisCloud API code left over from the original script.

Changes in 1.1.1:
- Removed an unsupported keyword from the time.set_value service call that
  Home Assistant rejected ("extra keys not allowed").
- entity_prefix is normalised, so "solis", "time.solis" and "sensor.solis"
  all resolve to the correct time entities.
- Slot 1 is checked before any writes, with a clear error if it is missing.
- Unavailable entities are treated as missing, so the script no longer
  reports success when the Solis Modbus integration is not polling.
- Writes and failures are counted and reported in the result.

Changes in 1.1.0:
- Writes go through the time.set_value service instead of the private
  hass.data["solis_modbus"]["time_entities"] structure, which broke in
  solis_modbus 4.2.x.
"""

VERSION = "2.0.0"

import json
from datetime import datetime, timedelta, timezone
import logging

log = logging.getLogger("pyscript.solis_modbus_smart_charging")
log.setLevel(logging.DEBUG)

SCHEDULE_SENSOR = "sensor.solis_modbus_charge_schedule"

MODE_LEGACY = "legacy"
MODE_SIX_SLOT = "six_slot"

# Six-slot (Grid Time of Use V2) firmware starts at HMI major version 0x4B,
# matching the 0x4B00 threshold used by the SolisCloud version of this script.
SIX_SLOT_HMI_MAJOR = 0x4B


# -----------------------------
# Entity helpers
# -----------------------------
def prefix_base(entity_prefix):
    """Normalise entity_prefix to its bare object-id part.

        "solis"              -> solis
        "time.solis"         -> solis
        "sensor.solis"       -> solis
    """
    prefix = str(entity_prefix).strip()
    if "." in prefix:
        return prefix.split(".", 1)[1]
    return prefix


def slot_time_entities(base, mode, slot):
    """Return (start_entity, end_entity) for a charge slot in the given mode."""
    if mode == MODE_SIX_SLOT:
        return (
            f"time.{base}_grid_time_of_use_charge_start_slot_{slot}",
            f"time.{base}_grid_time_of_use_charge_end_slot_{slot}",
        )
    return (
        f"time.{base}_time_charging_charge_start_slot_{slot}",
        f"time.{base}_time_charging_charge_end_slot_{slot}",
    )


def slot_switch_entity(base, slot):
    """Enable switch for a six-slot charge period."""
    return f"switch.{base}_grid_time_of_use_charging_period_{slot}"


def slot_current_entity(base, mode, slot):
    """Charge current number entity (per slot in six-slot, single in legacy)."""
    if mode == MODE_SIX_SLOT:
        return f"number.{base}_grid_time_of_use_charge_battery_current_slot_{slot}"
    return f"number.{base}_time_charging_charge_current"


def slot_soc_entity(base, slot):
    """Charge cut-off SOC number entity (six-slot only)."""
    return f"number.{base}_grid_time_of_use_charge_cut_off_soc_slot_{slot}"


def entity_state(entity_id):
    """Return the entity's state string, or None if it does not exist."""
    try:
        current = state.get(entity_id)
    except Exception:
        return None
    if current is None:
        return None
    return str(current)


def entity_exists(entity_id):
    """Return True if the entity is present and available.

    An unavailable entity is treated as missing: a blocking service call
    against one only logs a warning and writes nothing, so counting it as
    written would report success when the inverter was never updated.
    This is what happens when the Solis Modbus integration stops polling.
    """
    current = entity_state(entity_id)
    if current is None:
        return False
    return current.lower() != "unavailable"


def numbers_equal(current, target):
    """Compare a number entity's state with a target value."""
    try:
        return abs(float(current) - float(target)) < 0.001
    except (TypeError, ValueError):
        return False


# -----------------------------
# Firmware mode detection
# -----------------------------
def hmi_major_version(raw):
    """Turn the Solis Modbus HMI version sensor value into a major version.

    The Solis Modbus integration reports register 33002 as a plain number,
    for example 81 (0x51) for firmware SolisCloud shows as "5103". A value
    above 255 is treated as a full 16-bit version and its high byte used.
    Returns None if the value cannot be read.
    """
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in ("", "unknown", "unavailable", "none"):
        return None
    try:
        value = int(float(text))
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    if value <= 0xFF:
        return value
    return (value >> 8) & 0xFF


def detect_mode(base, force_mode):
    """Decide legacy or six-slot mode.

    Returns (mode, reason, hmi_raw). mode is None when the Solis Modbus
    entities are not ready yet (for example just after a Home Assistant
    restart); the caller then writes nothing rather than guess.
    """
    hmi_entity = f"sensor.{base}_hmi_version"
    hmi_raw = entity_state(hmi_entity)

    if force_mode == MODE_LEGACY:
        return MODE_LEGACY, "force_mode=legacy", hmi_raw
    if force_mode == MODE_SIX_SLOT:
        return MODE_SIX_SLOT, "force_mode=six_slot", hmi_raw

    if hmi_raw is None:
        log.warning(
            "No HMI version sensor (%s) found. Using legacy 3-slot mode. If your "
            "inverter has 6 charge slots, add \"force_mode\": \"six_slot\" to "
            "your automation config.",
            hmi_entity
        )
        return MODE_LEGACY, "no HMI version sensor", hmi_raw

    major = hmi_major_version(hmi_raw)
    if major is None:
        log.error(
            "The inverter HMI version (%s) is '%s', so the firmware type cannot be "
            "worked out yet. This is normal for a minute or two after Home Assistant "
            "restarts. Nothing will be written this time. To skip detection, add "
            "\"force_mode\" to your automation config.",
            hmi_entity, hmi_raw
        )
        return None, "HMI version not available yet", hmi_raw

    if major < SIX_SLOT_HMI_MAJOR:
        log.info(
            "HMI version %s (major 0x%02X) is below the six-slot threshold (0x%02X). "
            "Using legacy 3-slot mode.",
            hmi_raw, major, SIX_SLOT_HMI_MAJOR
        )
        return MODE_LEGACY, "HMI major 0x%02X below 0x%02X" % (major, SIX_SLOT_HMI_MAJOR), hmi_raw

    # Firmware says six-slot. Confirm the Grid Time of Use entities are there
    # and being read before trusting it: the Solis Modbus integration creates
    # them whenever its "V2 firmware" option is ticked, whatever the inverter
    # actually supports.
    probe_start, probe_end = slot_time_entities(base, MODE_SIX_SLOT, 1)
    probe_switch = slot_switch_entity(base, 1)
    missing = []
    not_ready = []
    for entity_id in (probe_start, probe_end, probe_switch):
        current = entity_state(entity_id)
        if current is None:
            missing.append(entity_id)
        elif current.lower() in ("unavailable", "unknown"):
            not_ready.append(f"{entity_id}={current}")
    if missing:
        log.warning(
            "HMI version %s (major 0x%02X) indicates six-slot firmware, but these Grid "
            "Time of Use entities do not exist: %s. Using legacy 3-slot mode. If your "
            "inverter has 6 charge slots, tick \"Updated to V2 Firmware\" in the Solis "
            "Modbus integration options.",
            hmi_raw, major, ", ".join(missing)
        )
        return MODE_LEGACY, "six-slot entities missing", hmi_raw
    if not_ready:
        log.error(
            "Six-slot firmware detected, but the Grid Time of Use entities are not "
            "being read yet (%s). Nothing will be written this time. If this persists, "
            "check the Solis Modbus integration is polling the inverter.",
            ", ".join(not_ready)
        )
        return None, "six-slot entities not available yet", hmi_raw

    return MODE_SIX_SLOT, "HMI major 0x%02X" % major, hmi_raw


# -----------------------------
# Dispatch window processing (shared with the SolisCloud version)
# -----------------------------
class WindowProcessor:
    def __init__(self, max_slots):
        self.max_slots = max_slots
        self.core_window = None
        self.dispatch_blocks = []

    def initialize_core_window(self, first_dispatch_time):
        """Initialise the core window from the first dispatch.

        Dispatches received between midnight and noon are treated as belonging
        to the previous evening's core window.
        """
        dispatch_tz = first_dispatch_time.tzinfo
        dispatch_hour = first_dispatch_time.hour

        if 0 <= dispatch_hour < 12:
            dispatch_date = (first_dispatch_time - timedelta(days=1)).date()
        else:
            dispatch_date = first_dispatch_time.date()

        next_date = dispatch_date + timedelta(days=1)

        core_start = datetime.combine(
            dispatch_date, datetime.strptime("23:30", "%H:%M").time()
        ).replace(tzinfo=dispatch_tz)
        core_end = datetime.combine(
            next_date, datetime.strptime("05:30", "%H:%M").time()
        ).replace(tzinfo=dispatch_tz)

        self.core_window = {"start": core_start, "end": core_end}
        log.debug("Initialised core window: %s to %s", core_start, core_end)

    def round_to_slot(self, dt, is_end_time=False):
        """Round a datetime to a 30-minute boundary (ends round up)."""
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

    def normalize_dispatch(self, dispatch):
        normalized = {
            "start": self.round_to_slot(dispatch["start"], False),
            "end": self.round_to_slot(dispatch["end"], True),
            "duration_minutes": (dispatch["end"] - dispatch["start"]).total_seconds() / 60,
        }
        for k, v in dispatch.items():
            if k not in ["start", "end"]:
                normalized[k] = v
        return normalized

    def normalize_dispatches(self, dispatches):
        """Normalise, sort, and merge contiguous dispatch windows."""
        if not dispatches:
            return []

        if self.core_window is None:
            self.initialize_core_window(dispatches[0]["start"])

        # Loops rather than comprehensions/lambdas for PyScript compatibility
        valid = []
        for d in dispatches:
            valid.append(self.normalize_dispatch(d))

        for i in range(len(valid)):
            for j in range(len(valid) - 1 - i):
                if valid[j]["start"] > valid[j + 1]["start"]:
                    valid[j], valid[j + 1] = valid[j + 1], valid[j]

        merged = []
        current = valid[0].copy()
        for nxt in valid[1:]:
            if (nxt["start"] - current["end"]).total_seconds() <= 1:
                current["end"] = max(current["end"], nxt["end"])
                current["duration_minutes"] = (current["end"] - current["start"]).total_seconds() / 60
            else:
                merged.append(current)
                current = nxt.copy()
        merged.append(current)

        for block in merged:
            log.debug("Dispatch block: %s to %s (%s mins)",
                      block["start"], block["end"], block["duration_minutes"])

        self.dispatch_blocks = merged
        return merged

    def process_core_hours(self):
        """Extend the core window over any overlapping dispatch blocks."""
        if not self.core_window:
            return

        while True:
            changes = False
            remaining = []
            for window in self.dispatch_blocks:
                overlaps = (
                    window["start"] <= self.core_window["end"]
                    and window["end"] >= self.core_window["start"]
                )
                if overlaps:
                    if window["start"] < self.core_window["start"]:
                        self.core_window["start"] = window["start"]
                        changes = True
                    if window["end"] > self.core_window["end"]:
                        self.core_window["end"] = window["end"]
                        changes = True
                else:
                    remaining.append(window)
            self.dispatch_blocks = remaining
            if not changes:
                break

        log.debug("Core window after processing: %s to %s",
                  self.core_window["start"], self.core_window["end"])

    def select_additional_windows(self):
        """Pick the longest remaining blocks to fill the slots after the core window."""
        if not self.dispatch_blocks:
            return []

        blocks = self.dispatch_blocks.copy()
        for i in range(len(blocks)):
            for j in range(len(blocks) - 1 - i):
                if blocks[j]["duration_minutes"] < blocks[j + 1]["duration_minutes"]:
                    blocks[j], blocks[j + 1] = blocks[j + 1], blocks[j]

        keep = max(0, self.max_slots - 1)
        if len(blocks) > keep:
            log.info("%s additional dispatch windows found, %s slots available - keeping the longest",
                     len(blocks), keep)
        return blocks[:keep]

    def format_windows(self, additional_windows):
        """Return exactly max_slots windows, core first, unused slots as 00:00-00:00."""
        if not self.core_window:
            self.initialize_core_window(datetime.now(timezone.utc))

        windows = [{
            "chargeStartTime": self.core_window["start"].strftime("%H:%M"),
            "chargeEndTime": self.core_window["end"].strftime("%H:%M"),
        }]

        for w in additional_windows:
            windows.append({
                "chargeStartTime": w["start"].strftime("%H:%M"),
                "chargeEndTime": w["end"].strftime("%H:%M"),
            })

        while len(windows) < self.max_slots:
            windows.append({
                "chargeStartTime": "00:00",
                "chargeEndTime": "00:00",
            })

        return windows[:self.max_slots]


def window_is_empty(window):
    return window["chargeStartTime"] == "00:00" and window["chargeEndTime"] == "00:00"


# -----------------------------
# Write planning and execution
# -----------------------------
def plan_operations(base, mode, windows, set_charge_current, charge_current,
                    set_charge_soc, charge_soc):
    """Build the list of writes needed.

    Each operation is a dict with: kind, slot, entity_id, target, domain,
    service, data. Entities whose current value already matches the target
    are marked "unchanged" and not written.
    """
    ops = []

    for slot in range(1, len(windows) + 1):
        window = windows[slot - 1]
        start_entity, end_entity = slot_time_entities(base, mode, slot)

        time_targets = [
            ("charge_start", start_entity, window["chargeStartTime"] + ":00"),
            ("charge_end", end_entity, window["chargeEndTime"] + ":00"),
        ]
        for kind, entity_id, target in time_targets:
            current = entity_state(entity_id)
            ops.append({
                "kind": kind,
                "slot": slot,
                "entity_id": entity_id,
                "current": current,
                "target": target,
                "domain": "time",
                "service": "set_value",
                "data": {"time": target},
                "unchanged": current == target,
            })

        if mode == MODE_SIX_SLOT:
            if set_charge_current:
                entity_id = slot_current_entity(base, mode, slot)
                current = entity_state(entity_id)
                ops.append({
                    "kind": "charge_current",
                    "slot": slot,
                    "entity_id": entity_id,
                    "current": current,
                    "target": charge_current,
                    "domain": "number",
                    "service": "set_value",
                    "data": {"value": charge_current},
                    "unchanged": numbers_equal(current, charge_current),
                })
            if set_charge_soc:
                entity_id = slot_soc_entity(base, slot)
                current = entity_state(entity_id)
                ops.append({
                    "kind": "charge_soc",
                    "slot": slot,
                    "entity_id": entity_id,
                    "current": current,
                    "target": charge_soc,
                    "domain": "number",
                    "service": "set_value",
                    "data": {"value": charge_soc},
                    "unchanged": numbers_equal(current, charge_soc),
                })
            # Switch the slot on after its times are written, so it never runs
            # with stale times. Slots without a window are left as they are.
            if not window_is_empty(window):
                entity_id = slot_switch_entity(base, slot)
                current = entity_state(entity_id)
                ops.append({
                    "kind": "enable",
                    "slot": slot,
                    "entity_id": entity_id,
                    "current": current,
                    "target": "on",
                    "domain": "switch",
                    "service": "turn_on",
                    "data": {},
                    "unchanged": current == "on",
                })

    if mode == MODE_LEGACY and set_charge_current:
        # Legacy firmware has a single charge current for all slots
        entity_id = slot_current_entity(base, mode, 1)
        current = entity_state(entity_id)
        ops.append({
            "kind": "charge_current",
            "slot": 0,
            "entity_id": entity_id,
            "current": current,
            "target": charge_current,
            "domain": "number",
            "service": "set_value",
            "data": {"value": charge_current},
            "unchanged": numbers_equal(current, charge_current),
        })

    return ops


def execute_operations(ops, inter_write_delay):
    """Run the writes. Returns (written, unchanged, failed_ops)."""
    written = 0
    unchanged = 0
    failed_ops = []

    for op in ops:
        if op["unchanged"]:
            unchanged = unchanged + 1
            log.debug("Unchanged: %s already %s", op["entity_id"], op["target"])
            continue

        if not entity_exists(op["entity_id"]):
            log.warning("Slot %s: %s not found or unavailable - skipping",
                        op["slot"], op["entity_id"])
            failed_ops.append({"kind": op["kind"], "slot": op["slot"],
                               "entity_id": op["entity_id"], "error": "not found or unavailable"})
            continue

        try:
            log.info("Writing %s slot %s: %s %s -> %s",
                     op["kind"], op["slot"], op["entity_id"], op["current"], op["target"])
            # Explicit keyword arguments per domain: PyScript forwards any
            # extra keyword into the service data, so nothing else is passed.
            if op["domain"] == "time":
                service.call("time", "set_value", blocking=True,
                             entity_id=op["entity_id"], time=op["target"])
            elif op["domain"] == "number":
                service.call("number", "set_value", blocking=True,
                             entity_id=op["entity_id"], value=op["target"])
            else:
                service.call("switch", "turn_on", blocking=True,
                             entity_id=op["entity_id"])
            written = written + 1
        except Exception as e:
            log.error("Error writing %s: %s", op["entity_id"], str(e))
            failed_ops.append({"kind": op["kind"], "slot": op["slot"],
                               "entity_id": op["entity_id"], "error": str(e)})

        if inter_write_delay > 0:
            task.sleep(inter_write_delay)

    return written, unchanged, failed_ops


def schedule_text(windows):
    parts = []
    for w in windows:
        if not window_is_empty(w):
            parts.append(f"{w['chargeStartTime']}-{w['chargeEndTime']}")
    return ", ".join(parts)


def publish_schedule(windows, attributes):
    """Publish the schedule and run details to sensor.solis_modbus_charge_schedule."""
    text = schedule_text(windows)
    attrs = {
        "charging_windows": windows,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "script_version": VERSION,
        "friendly_name": "Solis Modbus Charge Schedule",
        "icon": "mdi:battery-clock",
    }
    for k, v in attributes.items():
        attrs[k] = v
    try:
        state.set(SCHEDULE_SENSOR, value=text if text else "none", new_attributes=attrs)
    except Exception as e:
        log.warning("Could not update %s: %s", SCHEDULE_SENSOR, str(e))


def config_flag(config, key, default):
    return str(config.get(key, default)).lower() in ("true", "1", "yes", "on")


# -----------------------------
# Service
# -----------------------------
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

    missing = []
    for key in ("entity_prefix", "dispatch_sensor"):
        if key not in config or not str(config.get(key, "")).strip():
            missing.append(key)
    if missing:
        msg = f"Missing required configuration keys: {', '.join(missing)}"
        log.error(msg)
        return msg

    base = prefix_base(config["entity_prefix"])
    dispatch_sensor = str(config["dispatch_sensor"]).strip()
    force_mode = str(config.get("force_mode", "auto")).strip().lower()
    if force_mode not in ("auto", MODE_LEGACY, MODE_SIX_SLOT):
        log.warning("Unknown force_mode '%s' - using auto", force_mode)
        force_mode = "auto"
    diagnostics_only = config_flag(config, "diagnostics_only", "false")
    set_charge_current = config_flag(config, "set_charge_current", "false")
    set_charge_soc = config_flag(config, "set_charge_soc", "false")
    try:
        charge_current = float(config.get("charge_current", 60))
        charge_soc = int(float(config.get("charge_soc", 100)))
        inter_write_delay = float(config.get("inter_write_delay", 0.2))
    except (TypeError, ValueError) as e:
        msg = f"Invalid numeric configuration value: {str(e)}"
        log.error(msg)
        return msg

    log.info("Configuration: prefix=%s, force_mode=%s, diagnostics_only=%s, "
             "set_charge_current=%s (%s A), set_charge_soc=%s (%s%%)",
             base, force_mode, diagnostics_only, set_charge_current, charge_current,
             set_charge_soc, charge_soc)

    # ------------------------------------------------------------------
    # Dispatch sensor validation: fail before writing anything
    # ------------------------------------------------------------------
    dispatch_state = entity_state(dispatch_sensor)
    if dispatch_state is None:
        msg = (
            f"Dispatch sensor '{dispatch_sensor}' not found in Home Assistant. "
            "The entity ID varies by how Octopus Intelligent is set up: for "
            "account-linked chargers it is typically "
            "'binary_sensor.octopus_energy_<ACCOUNT_ID>_intelligent_dispatching'; "
            "for EV charger integrations (Hypervolt, Ohme, etc.) it may be "
            "'binary_sensor.octopus_energy_<EV_SERIAL>_intelligent_dispatching'. "
            "Search 'intelligent_dispatching' under Developer Tools > States."
        )
        log.error(msg)
        return "Dispatch sensor not found - check configuration"
    log.info("Dispatch sensor '%s' found (state: %s)", dispatch_sensor, dispatch_state)

    # ------------------------------------------------------------------
    # Firmware mode
    # ------------------------------------------------------------------
    mode, mode_reason, hmi_raw = detect_mode(base, force_mode)
    if mode is None:
        publish_schedule([], {"mode": "unknown", "mode_reason": mode_reason,
                              "hmi_version": hmi_raw, "last_result": "not_ready"})
        return f"Not ready: {mode_reason} - nothing written"
    max_slots = 6 if mode == MODE_SIX_SLOT else 3
    log.info("Mode: %s (%s), HMI version sensor: %s, slots: %s",
             mode, mode_reason, hmi_raw, max_slots)

    probe_start, probe_end = slot_time_entities(base, mode, 1)
    if not entity_exists(probe_start):
        msg = (
            f"Entity {probe_start} not found or unavailable. If it is missing, "
            f"check entity_prefix in your automation config - it should match the "
            f"Solis Modbus device naming, e.g. 'solis' for {probe_start.replace(base, 'solis', 1)}. "
            f"Look under Developer Tools > States and filter on 'charge_start_slot_1'. "
            f"If it exists but is unavailable, the Solis Modbus integration is not "
            f"polling the inverter - check its connection and logs."
        )
        log.error(msg)
        publish_schedule([], {"mode": mode, "mode_reason": mode_reason,
                              "hmi_version": hmi_raw, "last_result": "entity_missing"})
        return msg

    if set_charge_soc and mode == MODE_LEGACY:
        log.warning("set_charge_soc is only supported in six-slot mode - ignoring")
        set_charge_soc = False

    # ------------------------------------------------------------------
    # Charging windows
    # ------------------------------------------------------------------
    processor = WindowProcessor(max_slots)
    try:
        attrs = state.getattr(dispatch_sensor)
        if attrs and "planned_dispatches" in attrs and attrs["planned_dispatches"]:
            dispatches = attrs["planned_dispatches"]
            log.info("Processing %s planned dispatches", len(dispatches))
            processor.normalize_dispatches(dispatches)
            processor.process_core_hours()
            additional = processor.select_additional_windows()
            windows = processor.format_windows(additional)
        else:
            log.warning("No planned dispatches on %s - using the core window only", dispatch_sensor)
            windows = processor.format_windows([])
    except Exception as e:
        log.error(f"Error processing dispatch windows: {str(e)} - using the core window only")
        processor = WindowProcessor(max_slots)
        windows = processor.format_windows([])

    for i in range(len(windows)):
        log.info("Slot %s: %s-%s", i + 1, windows[i]["chargeStartTime"], windows[i]["chargeEndTime"])

    # ------------------------------------------------------------------
    # Plan and write
    # ------------------------------------------------------------------
    ops = plan_operations(base, mode, windows, set_charge_current, charge_current,
                          set_charge_soc, charge_soc)

    pending = []
    for op in ops:
        if not op["unchanged"]:
            pending.append({"kind": op["kind"], "slot": op["slot"], "entity_id": op["entity_id"],
                            "current": op["current"], "target": op["target"]})

    base_attrs = {
        "mode": mode,
        "mode_reason": mode_reason,
        "hmi_version": hmi_raw,
        "dispatch_sensor": dispatch_sensor,
    }

    if diagnostics_only:
        log.warning("=== DIAGNOSTICS MODE: not writing to the inverter ===")
        for p in pending:
            log.info("Would write %s slot %s: %s %s -> %s",
                     p["kind"], p["slot"], p["entity_id"], p["current"], p["target"])
        log.info("%s writes needed, %s already correct", len(pending), len(ops) - len(pending))
        attrs = dict(base_attrs)
        attrs["last_result"] = "diagnostics_only"
        attrs["pending_operations"] = pending
        publish_schedule(windows, attrs)
        return f"Diagnostics ({mode}): {len(pending)} writes needed, {len(ops) - len(pending)} already correct"

    if not pending:
        log.info("Charging schedule unchanged - nothing to write")
        attrs = dict(base_attrs)
        attrs["last_result"] = "unchanged"
        attrs["writes"] = 0
        publish_schedule(windows, attrs)
        return "Schedule unchanged - no update needed"

    written, unchanged, failed_ops = execute_operations(ops, inter_write_delay)

    attrs = dict(base_attrs)
    attrs["writes"] = written
    attrs["unchanged"] = unchanged
    attrs["failed_operations"] = failed_ops if failed_ops else None

    if failed_ops and not written:
        msg = f"Failed to write any changes ({len(failed_ops)} errors)"
        log.error(msg)
        attrs["last_result"] = "failed"
        publish_schedule(windows, attrs)
        return msg

    if failed_ops:
        msg = f"Updated charging schedule with {written} writes, {len(failed_ops)} failed"
        log.warning(msg)
        attrs["last_result"] = "partial_failure"
        publish_schedule(windows, attrs)
        return msg

    log.info(f"Successfully updated charging schedule ({written} writes, {unchanged} unchanged)")
    attrs["last_result"] = "success"
    publish_schedule(windows, attrs)
    return "Successfully updated charging schedule"
