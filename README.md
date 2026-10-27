# hOn — Quiet AC Commands

A small, HACS-installable fork of [gvigroux/hon](https://github.com/gvigroux/hon), based on the released **0.8.4** code. It keeps integration domain `hon`, existing config entries and entity identities. MIT license and upstream attribution are preserved.

## What this fork adds

Enable **Quiet AC commands from Home Assistant** in the integration options. One persistent boolean (`quiet_ac_commands`, default **off**) makes both hOn command-sending paths attach `echoStatus="1"` to the same outgoing AC command. It does not send a separate mute command, add a timer, change thermostat logic, or add another device controller. Other appliance types and the disabled setting retain their previous command parameters.

- Covers commands sent through this integration, including ordinary climate controls and low-level hOn services.
- The preference is independent of the cloud's reported Echo state. A reported Echo **on** does not prove that an outgoing HA command enables sound.
- The policy takes precedence over an explicit `echoStatus=0` while enabled. Disable the single integration option if audible HA commands are desired again.
- Changing the option itself does not start/stop an appliance or reload the integration; it applies to subsequent commands.
- Does **not** silence the physical remote, the hOn mobile app, mechanical noise, or hardware alarms.
- Offline transport/options tests and release validation are not physical acoustic acceptance. Confirm support on your own appliance; a rejected quiet command must not be retried automatically as an audible command.

**Requirements:** Home Assistant **2026.9.4 or newer**. Compatibility is tested against that Core version; newer versions may require revalidation. This is a replacement for the original integration, **not a second integration to install alongside it**.

[![GitHub](https://img.shields.io/github/license/gvigroux/hon?color=green)](https://github.com/gvigroux/hon/blob/main/LICENSE)

<a href="https://www.buymeacoffee.com/gvigroux"><img src="https://img.buymeacoffee.com/button-api/?text=Buy me a coffee&emoji=&slug=gvigroux&button_colour=5F7FFF&font_colour=ffffff&font_family=Cookie&outline_colour=000000&coffee_colour=FFDD00" /></a>

Home Assistant component supporting all devices integrated with hOn cloud.
- 2023/03/23 - I've decided to upgrade the main branch from the 'Full-Rework' branch. The old code is inside the legacy branch.
This move is mandatory to be able maange all devices (even unknown).
I hope that all the peoples that added their devices will test and upgrade this branch. Thanks :)
- 2023/04/12 - Change directory structure to be HACS compliant

## pre-requisite
Your appliances must be controlled by the hOn mobile application
supported device: Haier Climate tested

## Installation through HACS

1. Use your normal backup/rollback procedure and choose a maintenance window. Replacing Python integration code requires a Home Assistant restart and can affect every appliance in the hOn account.
2. If the original `gvigroux/hon` is managed by HACS, replace that **HACS repository registration**, rather than installing both repositories for domain `hon`. Prevent the old repository from overwriting this fork. **Do not delete the hOn integration/config entry from Settings → Devices & services.** If HACS requires removal of the old repository first, complete downloading this fork before restarting HA; removal may delete the old integration files.
3. In HACS, open **Custom repositories**, add `https://github.com/lisrec/hon` with category **Integration**, and download the published release of **hOn - Quiet AC Commands**.
4. Restart Home Assistant after the replacement files are downloaded.
5. For an existing installation, retain the existing hOn config entry; for a new installation, add **hOn - Quiet AC Commands** and authenticate with your own hOn account in HA.
6. Open the hOn integration's **Configure** options and enable **Quiet AC commands from Home Assistant**. Default is off for backward compatibility.
7. Confirm a representative command still works and is actually silent. Recheck after cloud refreshes; do not use the raw Echo switch as the acceptance criterion.

### Instalacja — skrót po polsku

- Repozytorium niestandardowe HACS: **https://github.com/lisrec/hon**, typ **Integracja**.
- Fork zastępuje dotychczasowy kod `hon`; nie dodawaj drugiego sterownika tego samego urządzenia i nie usuwaj istniejącego wpisu integracji z HA.
- Po podmianie kodu zrestartuj HA w uzgodnionym oknie serwisowym.
- W konfiguracji integracji włącz opcję cichych poleceń AC z Home Assistant. Opcja jest domyślnie wyłączona.
- Sprawdź działanie i rzeczywistą ciszę. Sam stan Echo nie jest testem akustycznym.

### Rollback

Disable the quiet-command option first if only the new policy must be withdrawn; this does not issue a device command. If code rollback is needed, restore the previously verified integration version/repository and its prior options, restart HA in a maintenance window, and verify current appliance modes/targets. Do not delete account configuration or restore unrelated HA data.

## Development and release validation

Use Python 3.14 and install `requirements-test.txt` into a dedicated virtual environment, then run `python -m pytest -q`. GitHub Actions also checks HACS metadata and hassfest. The release asset `hon_integration.zip` contains only the tracked `custom_components/hon` tree, rooted at the integration files; `SHA256SUMS` records its checksum. Source, manifest and release tag versions must agree.

This fork is MIT-licensed like the upstream project. Upstream device support, cloud protocol and original authorship remain credited below. Quiet-command support must be confirmed on the target appliances; installation readiness does not assert physical testing of every model.

## How to run any program?

You can launch any available program by using a dedicated service: `hon.start_program`.
To get all the details about each program, you can go to the device and click on `Get programs details`
![Get programs details](/images/device.jpg)

You will receive one notification per program, you just need to look and click at the notificaiton bell ![Bell](/images/bell.jpg)

Now you you can see all programs and all possible settings value. Have fun!
![Bell](/images/notification.jpg)

## You just want to update one settings?

You can repeat above process with the setting option and the service: `hon.update_settings`.

## Upstream compatibility list
This is the upstream project's device compatibility list, not a claim of physical quiet-command acceptance with this fork.

### Climate
- AS07TS4HRA-M
- AS25XCAHRA and AS35XCAHRA in 3x1 and 1x1 configuration with one/two outdoor units
- AS35TEDHRA(M1) and AS25TEDHRA(M1) in 2x1 configuration with one outdoor unit
- AS35S2SF1FA-WH and AS25S2SF1FA-WH in 2x1 configuration with one outdoor unit
- AS50S2SF2FA-1/1U50S2SJ2FA
- AD50S2SS1FA(H)

### Oven
- Candy Oven - FCT825XL WIFI Model

### Washing Machine
- HW 49AMC/1-80
- HW90-B14959S8U1
- hoover HWPDQ 49AMBC/1-S
- HW80-B14959TU1DE
- HW110-B14979U1

### Wine Cooler
- HWS42GDAU1
- HWS77GDAU1

### Dish Washer
- XIB 6B2D3FB
- HF 5E5D0FW-17

### WashDryer Machine
- HDQ 496AMBS/1-S

### Tumble Dryer
- Hoover H-Dry 350, 9 kg Condenser Tumble Dryer HRE C9TBE-80
- haier HD80-A3959
- HRE H9A2TE-S
- HLEH10A2TCEX-17
- HD90-A3Q979U1-S
- Candy ROE H9A3TCEX-S

### Air Purifier
- hoover HHP30C011 (Air Purifier 300)
- hoover HHP50CA011 (Air Purifier 500)

### Heat Pump Water Heater
- Haier HP150M8-9 (only programs are working)

### Air to Water Heat Pump
- Haier Monobloc GT R290
