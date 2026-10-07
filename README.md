# Solis Modbus Smart Charging for Home Assistant

This integration synchronizes Solis inverter charging windows with Octopus Energy Intelligent dispatch periods in Home Assistant using Modbus TCP communication. It automatically adjusts your battery charging schedule to maximize the use of cheaper electricity during dispatch periods while maintaining core charging hours.

It is based on the original SolisCloud API version found here: https://github.com/Moondevil-ha/solis-smart-charging

## Features

- Automatically syncs Solis inverter charging windows with Octopus Energy Intelligent dispatch periods
- Maintains protected core charging hours (23:30-05:30)
- Supports both Solis charge slot layouts, detected automatically:
  - **Legacy firmware:** 3 charge slots ("Time-Charging")
  - **Six-slot firmware:** 6 charge slots ("Grid Time of Use"), each with its own enable switch
- Only writes values that have actually changed
- Diagnostics mode to check what it would write before letting it write anything
- Optional per-slot charge current and charge cut-off SOC
- Publishes the schedule and the result of each run to `sensor.solis_modbus_charge_schedule`
- Smart charging window management:
  - Automatically detects and merges contiguous charging blocks
  - Extends core hours when dispatch periods are adjacent
  - Handles early charging completion appropriately
  - Maintains charging windows during dispatch periods
- Robust time handling:
  - All times normalized to 30-minute slots
  - Smart handling of overnight periods and early morning dispatches
  - Timezone-aware datetime processing
  - Proper management of charging windows across midnight boundary

## Prerequisites

- Home Assistant installation
- Solis inverter with battery storage and Modbus TCP enabled
- Octopus Energy Intelligent tariff
- [Octopus Energy Integration](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy) installed
- [pyscript integration](https://github.com/custom-components/pyscript) installed
- [Solis Modbus Integration](https://github.com/Pho3niX90/solis_modbus) installed and configured, using the "Full" poll profile (the "Essential only" profile does not create the charge slot entities)
- For six-slot firmware, "Updated to V2 Firmware" ticked in the Solis Modbus integration

## Installation

1. Ensure you have the pyscript integration installed and configured in Home Assistant
2. Add the following to your `configuration.yaml`:

```yaml
pyscript:
  allow_all_imports: true
  hass_is_global: true
```

3. Copy `solis_modbus_smart_charging.py` to your `config/pyscript` directory
4. Add the automation to your `automations.yaml` or through the Home Assistant UI, setting `entity_prefix` and `dispatch_sensor` to match your own entities (see below). No changes to the script itself are needed.

## Configuration

### Automation

```yaml
alias: Sync Solis Charging with Octopus Dispatch
description: ""
triggers:
  - trigger: state
    entity_id:
      - binary_sensor.octopus_energy_a_42185595_intelligent_dispatching
    attribute: planned_dispatches
conditions:
  - condition: template
    value_template: >
      {% set dispatches =
      state_attr('binary_sensor.octopus_energy_a_42185595_intelligent_dispatching',
      'planned_dispatches') %} {% if dispatches is none %}
        {% set result = false %}
      {% else %}
        {% set result = true %}
      {% endif %} {{ result }}
actions:
  - action: pyscript.solis_modbus_smart_charging
    metadata: {}
    data:
      config: |-
        {
          "entity_prefix": "solis",
          "dispatch_sensor": "binary_sensor.octopus_energy_a_42185595_intelligent_dispatching"
        }
mode: single
```
**Setting `dispatch_sensor`:** the dispatching sensor name usually includes your Octopus account ID (or your charger's serial number if you have an EV charger such as Hypervolt or Ohme). Check the exact name under Developer Tools > States.

**Setting `entity_prefix`:** this must match how the Solis Modbus integration has named your entities. In Developer Tools > States, filter on `charge_start_slot_1`. Your prefix is everything before `_time_charging_...` or `_grid_time_of_use_...`:

| Entity shown in Home Assistant | `entity_prefix` to use |
|---|---|
| `time.solis_time_charging_charge_start_slot_1` | `solis` |
| `time.solis_s6_solis_time_charging_charge_start_slot_1` | `solis_s6_solis` |
| `time.solis_s5_eh1p_grid_time_of_use_charge_start_slot_1` | `solis_s5_eh1p` |

The `time.` part is optional, so `solis` and `time.solis` both work. If the prefix is wrong, the script stops before writing anything and logs the entity name it was looking for.

### Options

All options go in the `config` block alongside `entity_prefix` and `dispatch_sensor`.

| Option | Default | What it does |
|---|---|---|
| `entity_prefix` | required | Prefix of your Solis Modbus entities (see above) |
| `dispatch_sensor` | required | Your Octopus `intelligent_dispatching` binary sensor |
| `force_mode` | `auto` | `auto` detects the slot layout; `legacy` forces 3 slots; `six_slot` forces 6 slots |
| `diagnostics_only` | `false` | Works out the writes and reports them in the log and on `sensor.solis_modbus_charge_schedule`, without writing anything |
| `set_charge_current` | `false` | Also set the charge current. Six-slot: every slot's current. Legacy: the single Time-Charging current |
| `charge_current` | `60` | Charge current in amps, used when `set_charge_current` is on |
| `set_charge_soc` | `false` | Six-slot only: also set every slot's charge cut-off SOC |
| `charge_soc` | `100` | Charge cut-off SOC in percent, used when `set_charge_soc` is on |
| `inter_write_delay` | `0.2` | Seconds to pause between Modbus writes |

Example with diagnostics on, for a first run on a new setup:

```json
{
  "entity_prefix": "solis_s5_eh1p",
  "dispatch_sensor": "binary_sensor.octopus_energy_a_42185595_intelligent_dispatching",
  "diagnostics_only": true
}
```

### How the slot layout is detected

1. If `force_mode` is set, that is used.
2. Otherwise the script reads the inverter's HMI version from `sensor.<prefix>_hmi_version`. Version `0x4B` or above (shown as 75 or above in Home Assistant) means six-slot firmware. This is the same threshold as the SolisCloud version of this script.
3. Before using six-slot mode it checks the Grid Time of Use entities exist and are being read. The Solis Modbus integration creates them whenever "Updated to V2 Firmware" is ticked, whatever the inverter actually supports.
4. If the Grid Time of Use entities do not exist, it uses legacy mode. If the HMI version or those entities are not being read yet (normal for a minute or two after Home Assistant restarts), it writes nothing and tries again on the next dispatch update.

The mode chosen and the reason are logged, and shown on `sensor.solis_modbus_charge_schedule`.

## How It Works

1. The script monitors Octopus Energy Intelligent dispatch periods
2. When dispatch periods are updated:
   - Core charging hours (23:30-05:30) are protected and cannot be reduced
   - Early morning dispatches (00:00-12:00) are processed against previous day's core window
   - The script identifies contiguous charging blocks and merges them
   - Core hours are extended if dispatch periods are adjacent
   - Additional charging windows are selected based on available charge amount
   - All times are normalized to 30-minute slots
3. During charging:
   - Dispatch windows may remain but with adjusted kWh values
   - Binary sensor state indicates valid charging periods
   - Windows automatically adjust based on actual charging needs
4. The resulting charging windows are written directly to your Solis inverter via Modbus: 3 windows on legacy firmware, 6 on six-slot firmware
5. The process repeats when new dispatch periods are received

## Known Behaviors

1. Dispatch Windows:
   - Windows may remain after charging completion
   - System maintains window integrity during overnight transitions

2. Window Processing:
   - Early morning dispatches (before 12:00) align with previous day's core window
   - Windows are always normalized to 30-minute boundaries
   - Core window can extend but never shrink

3. Modbus Communication:
   - Connection failures are handled gracefully with error logging
   - Updates are processed individually to prevent complete failure if one update fails
   - Each slot time is written through Home Assistant's `time.set_value` service, the same route as changing it by hand in the UI
   - Unavailable slot entities are skipped and reported as failures rather than counted as written
   - Values that already match are not rewritten, so a run with no changes makes no Modbus writes

4. Six-slot enable switches:
   - On six-slot firmware each charge slot also has an on/off switch. The script switches on every slot it gives a charging window, after writing that slot's times
   - It never switches a slot off. Unused slots are set to 00:00-00:00, so they do nothing even if switched on
   - Discharge slots are not touched

5. Inverter clock:
   - The Solis Modbus integration corrects inverter clock drift itself, so this script does not set the inverter time

## Troubleshooting

1. Check your Modbus connection:
   - Verify your inverter's IP address is correct in the Solis Modbus integration
   - Ensure port 502 is open and accessible
   - Check the Solis Modbus integration logs for connection issues

2. Common Issues:
   - If you see connection errors, verify your network connectivity to the inverter
   - If time updates fail, check that your Modbus write permissions are correct
   - If windows aren't updating, check the Octopus dispatch sensor is providing data
   - "Entity ... not found or unavailable": check `entity_prefix` as described under Configuration. If the entity exists but shows as unavailable, the Solis Modbus integration is not polling the inverter

3. Solis Modbus 4.x upgrade trap:
   - If the Solis Modbus integration was set up without the inverter's serial number, version 4.x defers its migration on every restart, creates duplicate devices, and never starts polling. Every charge slot entity then shows as unavailable.
   - Re-adding the integration on top of the old entry can fail with `DeviceIdentifierCollisionError`.
   - The fix is to remove the Solis Modbus integration entry completely, then add it again with the serial number entered. Your `entity_prefix` may change afterwards, so check it again.

4. "Not ready" results:
   - The script writes nothing when it cannot yet read the inverter's HMI version or the Grid Time of Use entities, which is normal shortly after Home Assistant restarts. It runs again on the next dispatch update
   - If it persists, check the Solis Modbus integration is connected and polling

5. Wrong slot layout:
   - If the script picks legacy mode on an inverter with 6 charge slots (or the reverse), the log shows why. Set `force_mode` to override

6. Data logger and SolisCloud:
   - On older S2-WL-ST data logger firmware, any Modbus TCP connection on port 502 stops the logger reporting to SolisCloud. Later firmware supports both at once; Solis support can update the logger remotely on request.

## Example Dashboard View

`sensor.solis_modbus_charge_schedule` shows the current schedule (for example `23:30-05:30, 13:00-14:30`) with these attributes: `charging_windows`, `mode`, `mode_reason`, `hmi_version`, `writes`, `unchanged`, `failed_operations`, `last_result`, and `pending_operations` in diagnostics mode.

See the original README for more dashboard examples.

## Upgrading from v1.x

- `dispatch_sensor` is now required. Every documented automation already sets it
- Inverters with six-slot firmware now use the Grid Time of Use slots automatically. Inverters with legacy firmware carry on as before
- Results now appear on `sensor.solis_modbus_charge_schedule`
- If you also run the SolisCloud version of this script, only one of the two should be writing to the inverter at a time

## Version History

### v2.0.0
- Six-slot (Grid Time of Use) firmware support, with automatic detection and a `force_mode` override
- Switches on the enable switch for every six-slot charge slot given a window; never switches slots off
- Only writes values that have changed
- `diagnostics_only` mode
- Optional per-slot charge current and charge cut-off SOC
- Publishes `sensor.solis_modbus_charge_schedule`
- `dispatch_sensor` is required and checked before anything is written
- Removed unused SolisCloud API code

### v1.1.1
- Fixed the `time.set_value` service call, which Home Assistant was rejecting with "extra keys not allowed"
- `entity_prefix` now works with or without the `time.` domain
- Checks the first charge slot entity exists before writing, with a clear error if not
- Unavailable entities are skipped and reported, so the script no longer reports success when the Solis Modbus integration is not polling
- Reports how many slot times were written and how many failed

### v1.1.0
- Writes now go through Home Assistant's `time.set_value` service instead of the Solis Modbus integration's internal data, which broke in Solis Modbus 4.2.x

### v1.0
- Initial release with Modbus TCP control
- Complete rewrite of window handling for local control
- Enhanced error handling for Modbus communication
- Improved logging and status reporting

## Contributing

Developed with the help of Rose Nightingale, who performed all the testing of modbus functionality.
Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the LICENSE file for details.
