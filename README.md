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
4. There are a couple of references in the code to the dispatch entity. Ensure you change this to match your own entities.
5. Add the automation to your `automations.yaml` or through the Home Assistant UI

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
          "entity_prefix": "solis_modbus",
          "dispatch_sensor": "binary_sensor.octopus_energy_a_42185595_intelligent_dispatching"
        }
mode: single
```
Note: The dispatching sensor will usually include your account ID, please check and edit the automation appropriately for the correct entity.
Note2: You will need to change the entity prefix to match your specific setup.

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
   - Local entities reflect the intended state even if Modbus communication fails

## Troubleshooting

1. Check your Modbus connection:
   - Verify your inverter's IP address is correct in the Solis Modbus integration
   - Ensure port 502 is open and accessible
   - Check the Solis Modbus integration logs for connection issues

2. Common Issues:
   - If you see connection errors, verify your network connectivity to the inverter
   - If time updates fail, check that your Modbus write permissions are correct
   - If windows aren't updating, check the Octopus dispatch sensor is providing data
   - Check your entity names are reflected correctly in the pyscript, and the automation

## Example Dashboard View

See the original README for dashboard examples - they work the same way with the Modbus implementation.

## Version History

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
