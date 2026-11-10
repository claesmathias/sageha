# Sage Coffee Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![Tests](https://github.com/simonjgreen/sageha/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/simonjgreen/sageha/actions/workflows/tests.yml)
[![Validate](https://github.com/simonjgreen/sageha/actions/workflows/validate.yaml/badge.svg?branch=main)](https://github.com/simonjgreen/sageha/actions/workflows/validate.yaml)

Home Assistant custom integration for Sage/Breville connected coffee machines.

## Features

- **Power Control**: Turn your coffee machine on (wake) and off (sleep)
- **Real-time State Monitoring**: See if your machine is ready, warming up, or asleep
- **Temperature Sensors**: Monitor brew and steam boiler temperatures
- **Configuration Sensors**: View theme, brightness, grind size, and more
- **Wake Schedule**: See when your machine is next scheduled to switch on automatically
- **Firmware Version**: Check the installed firmware version from the device page
- **Live Activity**: Follow the warm-up on your phone's Lock Screen through the companion app

## Supported Machines

This integration uses the [sagecoffee](https://github.com/simonjgreen/sagecoffee) library and supports WiFi-connected Sage/Breville machines. At time of publication this is only:

- Sage/breville Oracle Dual Boiler (BES995)

## Installation

### HACS (Recommended)

Sage Coffee is included in the default HACS repository, so no custom repository setup is required.

1. Open HACS in Home Assistant
2. Search for "Sage Coffee"
3. Click on it and select "Download"
4. Restart Home Assistant

### Manual Installation

1. Download the `custom_components/sagecoffee` folder from this repository
2. Copy it to your Home Assistant `config/custom_components/` directory
3. Restart Home Assistant

## Configuration

1. Go to **Settings** → **Devices & Services**
2. Click **Add Integration**
3. Search for "Sage Coffee"
4. Choose your authentication method:
   - **Email and Password**: Enter your Sage/Breville account credentials. If your account has two-step verification enabled, you will then be asked for the code from your authenticator app
   - **Refresh Token**: If you already have a refresh token from the sagectl CLI tool
   - **Machine Type**: Select Sage (Europe), Breville (Rest of the world) or Lelit

### Getting a Refresh Token (Advanced)

If you prefer not to enter your password in Home Assistant, you can use the sagectl CLI tool to obtain a refresh token:

```bash
pip install sagecoffee
sagectl bootstrap --username your.email@example.com
```

Then paste the refresh token from `~/.config/sagecoffee/config.toml` into Home Assistant.

### Live Activity

The integration can show the machine warming up as a Live Activity on iOS (Lock Screen and Dynamic Island) or a Live Update on Android, through the Home Assistant companion app. To turn it on, go to **Settings → Devices & services → Sage Coffee → Configure** and set:

- **Show warm-up Live Activity**: on or off
- **Devices**: the companion app devices to show it on
- **Keep "Ready" visible for**: how many minutes the activity stays after the machine is ready (0 ends it immediately)
- **Update every**: the shortest time between temperature updates; 0 (the default) sends every change straight away
- **Show steam boiler temperature**: on dual boiler machines, show the steam boiler on a second line (off by default)

The activity starts when the machine begins warming up, shows the brew boiler's progress towards its target temperature, changes to "Ready to brew" when the machine is ready, and ends if the machine goes back to sleep. Temperatures arrive by push from the machine, which sends a report about every 15 seconds while warming up, so a warm-up from cold takes around 15 updates. With **Update every** at 0 each report is shown straight away. Progress updates are sent at low priority, which iOS may delay or batch, and iOS may throttle activities that update very often.

To check that it works without waking the machine, press the **Test Live Activity** button on the machine's device page: it shows a simulated warm-up, then "Ready to brew", and ends after about 30 seconds.

This needs iOS 17.2 or later, or Android 16 or later, and a companion app version with Live Activity support. See the [companion app documentation](https://companion.home-assistant.io/docs/notifications/live-activities) for details. Away from home, the app needs a working remote connection for the activity to update.

## Entities

For each coffee machine, the integration creates:

### Switch

| Entity | Description                               |
| ------ | ----------------------------------------- |
| Power  | Turn the machine on (wake) or off (sleep) |

### Button

| Entity             | Description                                                              |
| ------------------ | ------------------------------------------------------------------------ |
| Test Live Activity | Plays a short simulated warm-up on the Live Activity devices (~30 s)     |

### Text

| Entity | Description                               |
| ------ | ----------------------------------------- |
| Name   | Set appliance name                        |

### Select

| Entity | Description                               |
| ------ | ----------------------------------------- |
| Theme  | Set theme (dark/light)                    |

### Light

| Entity     | Description                                      |
| ---------- | ------------------------------------------------ |
| Work Light | Cup warmer illumination — on/off with brightness |

### Number

| Entity             | Description                       |
| ------------------ | --------------------------------- |
| Display Brightness | Set display brightness percentage |
| Volume             | Set volume level percentage       |

### Sensors

| Entity                   | Description                                    |
| ------------------------ | ---------------------------------------------- |
| State                    | Current machine state (ready, warming, asleep) |
| Brew Temperature         | Current brew boiler temperature                |
| Brew Target Temperature  | Target brew boiler temperature                 |
| Steam Temperature        | Current steam boiler temperature (dual boiler) |
| Steam Target Temperature | Target steam boiler temperature (dual boiler)  |
| Theme                    | Display theme (dark/light)                     |
| Display Brightness       | Display brightness percentage                  |
| Grind Size               | Current grinder setting                        |
| Volume                   | Volume level percentage                        |
| Auto-off Time            | Idle time before auto-sleep (minutes)          |
| Next Wake Time           | Next scheduled automatic wake time             |
| Wake Schedule            | Enabled wake times, e.g. `Weekdays 06:30 · Weekends 08:00`; every schedule (also disabled ones) is listed in the `schedules` attribute |
| Temperature Unit         | Configured temperature unit                    |
| Timezone                 | Configured machine timezone                    |
| Remote Wake              | Whether remote wake is enabled                 |
| Last Paired              | Last cloud pairing timestamp                   |
| State Report Version     | Latest WebSocket state report version          |
| Firmware Version         | Installed firmware version (diagnostic)        |
| MCU Firmware Version     | Installed MCU firmware version (diagnostic)    |
| OTA Service Version      | Installed OTA service version (diagnostic)     |
| SOM Image Version        | Installed SOM image version (diagnostic)       |
| Errors Count             | Number of appliance errors; names in `errors`  |
| Wi-Fi Signal Strength    | Reported Wi-Fi signal strength (diagnostic)    |

## Actions

### Enable wake schedule
```yaml
action: sagecoffee.set_wake_schedule
data:
  serial: YOUR_SERIAL_NUMBER
  hours: 7
  minutes: 30
  days:
    - mon
    - tue
    - wed
    - thu
    - fri
```

The `days` field is optional. If omitted, the schedule runs every day. The `enabled` field (default `true`) can be set to `false` to create a disabled schedule.

Existing schedules, including ones created in the Sage/Breville app, are kept: the new schedule is added alongside them, and calling the action again with the same time and days enables or disables that schedule.

### Disable wake schedule

```yaml
action: sagecoffee.disable_wake_schedule
data:
  serial: YOUR_SERIAL_NUMBER
```

## Automations

Here are some example automations you can create:

### Wake up machine before your morning alarm

```yaml
automation:
  - alias: "Morning Coffee Warmup"
    trigger:
      - platform: time
        at: "06:30:00"
    condition:
      - condition: time
        weekday:
          - mon
          - tue
          - wed
          - thu
          - fri
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.oracle_dual_boiler_power
```

### Notification when machine is ready

```yaml
automation:
  - alias: "Coffee Machine Ready"
    trigger:
      - platform: state
        entity_id: sensor.oracle_dual_boiler_state
        to: "ready"
    action:
      - service: notify.mobile_app
        data:
          message: "Coffee machine is ready!"
```

## Troubleshooting

### Authentication Issues

- Ensure your Sage/Breville account credentials are correct
- Try using the sagectl CLI tool to verify your credentials work
- Check that your machine is connected to WiFi and visible in the Sage/Breville app

### Device appears in app but not in integration

If you can see your machine in the official Sage/Breville mobile app but it's not appearing in Home Assistant:

- Check that you're using a **regular email and password account** for authentication
- Google account sign-in does not bind your identities between the app and the integration
- If you only have a Google account, consider creating a password-protected account through the Sage/Breville account settings and use that for Home Assistant

### Connection Issues

- The integration uses WebSocket for real-time updates; ensure your Home Assistant instance can reach `iot-api-ws.breville.com`
- Check Home Assistant logs for detailed error messages

### Machine Not Found

- Ensure your machine is paired with your Sage/Breville account
- Try re-pairing the machine using the official Sage/Breville app

## Removal

1. Go to **Settings** → **Devices & Services**
2. Find "Sage Coffee" and click on it
3. Click the three dots menu and select **Delete**
4. If installed via HACS, also remove it there: go to **HACS** → find "Sage Coffee" → click the three dots menu → **Remove**
5. Restart Home Assistant if prompted

## Contributing

Contributions are welcome! Please open an issue or pull request on the [GitHub repository](https://github.com/simonjgreen/sageha).

## License

This project is licensed under the MIT License.

## Disclaimer

This is an unofficial integration. It is not affiliated with, endorsed by, or supported by Breville or Sage. Use at your own risk.
