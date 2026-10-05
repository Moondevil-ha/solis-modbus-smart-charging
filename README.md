# Solis Modbus Smart Charging for Home Assistant

This integration synchronizes Solis inverter charging windows with Octopus Energy Intelligent dispatch periods in Home Assistant using Modbus TCP communication. It automatically adjusts your battery charging schedule to maximize the use of cheaper electricity during dispatch periods while maintaining core charging hours.

It is based on the original SolisCloud API version found here: https://github.com/Moondevil-ha/solis-smart-charging

## Features

- Automatically syncs Solis inverter charging windows with Octopus Energy Intelligent dispatch periods
- Maintains protected core charging hours (23:30-05:30)
- Supports up to three charging windows (Solis limitation)
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
- [Solis Modbus Integration](https://github.com/Pho3niX90/solis_modbus) installed and configured

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

**Setting `entity_prefix`:** this must match how the Solis Modbus integration has named your charge slot entities. In Developer Tools > States, filter on `time_charging_charge_start_slot_1`. Your prefix is everything before `_time_charging_charge_start_slot_1`:

| Entity shown in Home Assistant | `entity_prefix` to use |
|---|---|
| `time.solis_time_charging_charge_start_slot_1` | `solis` |
| `time.solis_s6_solis_time_charging_charge_start_slot_1` | `solis_s6_solis` |

From v1.1.1 the `time.` part is optional, so `solis` and `time.solis` both work. If the prefix is wrong, the script stops before writing anything and logs the entity name it was looking for.

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
4. The resulting charging windows are written directly to your Solis inverter via Modbus
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

4. Data logger and SolisCloud:
   - On older S2-WL-ST data logger firmware, any Modbus TCP connection on port 502 stops the logger reporting to SolisCloud. Later firmware supports both at once; Solis support can update the logger remotely on request.

## Example Dashboard View

See the original README for dashboard examples - they work the same way with the Modbus implementation.

## Version History

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
