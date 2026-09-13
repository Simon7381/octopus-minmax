# Octopus Minmax Bot 🐙🤖

## Description

This bot will use your electricity usage and compare your current Smart tariff costs for the day with another smart tariff and initiate a switch if it's cheaper. See below for supported tariffs.

Due to how Octopus Energy's Smart tariffs work, switching manually makes the _new_ tariff take effect from the start of the day. For example, if you switch at 11 PM, the whole day's costs will be recalculated based on your new tariff, allowing you to potentially save money by tariff-hopping.

I created this because I've been a long-time Agile customer who got tired of the price spikes. I now use this to enjoy the benefits of Agile (cheap days) without the risks (expensive days).

I personally have this running automatically every day at 11 PM inside a Raspberry Pi Docker container, but you can run it wherever you want. It sends notifications and updates to a variety of services via [Apprise](https://github.com/caronc/apprise), but that's not required for it to work.

## Web Dashboard

After starting the bot you can access the web dashboard on `localhost:5050`

- Make changes to your config through the dashboard without needing to restart
- Access and read logs
- See graph of savings (coming soon)

## How to Use

### Requirements

- An Octopus Energy Account
  - In case you don't have one, we both get £50 for using my referral: https://share.octopus.energy/coral-lake-50
  - Get your API key [here](https://octopus.energy/dashboard/new/accounts/personal-details/api-access)
- A smart meter
- Be on a supported Octopus Smart Tariff (see tariffs below)
- An Octopus Home Mini for real-time usage (**Important**). Request one from Octopus Energy for free [here](https://octopus.energy/blog/octopus-home-mini/).

### HomeAssistant Addon

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FSimon7381%2Foctopus-minmax)

OR

To install this third-party add-on:

1. Open Home Assistant > Settings > Add-ons > Add-on Store.
2. Click the menu (three dots in the top-right corner) and select Repositories.
3. Paste the GitHub repository link into the field at the bottom:
   https://github.com/eelmafia/octopus-minmax
4. Refresh the page if needed. The add-on will appear under **Octopus MinMax Bot**.

### Running Manually

1. Install the Python requirements.
   Use Python 3.12 and run `python -m invisible_playwright fetch`. The container includes C++ patched Firefox
   and its runtime dependencies; see [invisible-playwright documentation](https://github.com/feder-cr/invisible_playwright).
2. Configure the environment variables.
3. Run `main.py`. I recommend scheduling it to run it at 11 PM in order to leave yourself an hour as a safety margin in case Octopus takes a while to generate your new agreement.

### Running using Docker

Docker run command:

```
docker run -d \
  --name MinMaxOctopusBot \
  -p 5050:5050 \
  -v ./logs:/app/logs \
  -e ACC_NUMBER="<your_account_number>" \
  -e API_KEY="<your_api_key>" \
  -e EXECUTION_TIME="23:30" \
  -e SWITCH_THRESHOLD=2 \
  -e NOTIFICATION_URLS="<apprise_notification_urls>" \
  -e ONE_OFF=false \
  -e DRY_RUN=false \
  -e TARIFFS=go,agile,flexible \
  -e TZ=Europe/London \
  -e BATCH_NOTIFICATIONS=false \
  -e WEB_USERNAME="<whatever_you_want>" \
  -e WEB_PASSWORD="<whatever_you_want>" \
  eelmafia/octopus-minmax-bot
```

or use the docker-compose.yaml **Don't forget to add your environment variables**

### Website fallback for tariff switches

The bot first tries the tariff initiation API. If it fails, it signs in to the Octopus
website using `OCTOPUS_LOGIN_EMAIL` and `OCTOPUS_LOGIN_PASSWD`. Permission error
`KT-CT-1111` triggers the fallback immediately. Other API failures retain the existing
retry policy. Agreement acceptance continues through the API unless Octopus has
already completed the new enrolment on the website.

| Tariff ID | Signup path    | Website action                                                           |
| --------- | -------------- | ------------------------------------------------------------------------ |
| `go`      | `go`           | Check I accept the Terms & Conditions, then Switch Tariff                |
| `agile`   | `agile`        | Check terms if displayed, then Switch Tariff                             |
| `cosy`    | `cosy-octopus` | Select Variable explicitly, check terms if displayed, then Switch Tariff |

Each path opens `https://octopus.energy/smart/<path>/sign-up/?accountNumber=<account>`
and follows Octopus's redirect to its current signup route.
The browser opens `https://octopus.energy/dashboard/`, follows Octopus's redirect to
`https://auth.octopus.energy/login/`, submits the current login form, then loads
`/dashboard/` again and verifies the configured account number in
the resulting URL or page source before inspecting or submitting a signup form.
Only `go`, `agile` and `cosy` have website routes. `go-fix-12m` is supported through
GraphQL only; if its API initiation fails, the bot reports an error without opening
the browser. The Go website route accepts the displayed terms without selecting a variant.

The fallback checks for an existing matching pending enrolment before submitting,
then polls for a new enrolment for the exact target product for two minutes.
Missing or ambiguous enrolments cause an error; check the account and emails before
retrying a submitted switch. This also requires read access to `productEnrolments`.

Set website credentials in the container environment, Home Assistant add-on options,
or the dashboard. A blank password field in the dashboard keeps the saved password;
dashboard changes reset on restart. For Compose, put these two environment variables
in a local `.env` file or export them before starting Compose.

Container images support `amd64` and `arm64` (64-bit); the browser fallback does not
support the old 32-bit ARM add-on targets.

### Testing on Windows with Podman Desktop

Set `TEST_PLAYWRIGHT=true` in your Compose environment and recreate the container to
test the live website login and all three signup pages. This startup mode takes
precedence over `DRY_RUN`, `ONE_OFF`, the configured tariff list and scheduling.
It checks Go's terms checkbox, any displayed Agile terms, Cosy's Variable option,
and whether each Switch Tariff button is actionable using a trial click that does
not submit. It does not initiate switches or call the agreement acceptance API.
Results are logged per tariff; failed signup checks also save a screenshot under
`logs/playwright-<tariff>-failure.png`. The dashboard remains running afterwards.
Set `TEST_PLAYWRIGHT=false` and recreate the container to resume normal operation.
This mode writes results locally and does not send external notifications.
Octopus protects login with invisible hCaptcha and may present an interactive image
challenge to a headless browser. When that happens, the test and live fallback stop
without submitting a switch and log the challenge explicitly; a previously authenticated
browser session or an Octopus login flow that does not challenge automation is required.

1. Finish Podman Desktop onboarding and start its Podman machine. Windows needs a
   Linux VM provided by WSL 2 or Hyper-V; follow the
   [Podman Windows setup guide](https://podman-desktop.io/docs/installation/windows-install).
2. From this repository, build the changed source and run the offline tests:

   ```powershell
   podman info
   podman build -f dockerfile -t localhost/octopus-minmax:playwright .
   podman run --rm localhost/octopus-minmax:playwright python -m unittest discover -s tests -v
   ```

   These tests use mock API responses and local HTML in C++ patched Firefox, with no Octopus login
   or tariff changes. Commands use Podman's documented [build](https://docs.podman.io/en/latest/markdown/podman-build.1.html)
   and [run](https://docs.podman.io/en/latest/markdown/podman-run.1.html) options.

3. Copy `podman.env.example` to `podman.env` and enter your API key, account number,
   website credentials and dashboard password. Keep `DRY_RUN=true` and `ONE_OFF=true`.
   `podman.env` is excluded from Git and container build context.
4. Start a comparison against your account:

   ```powershell
   podman run -d --name octopus-minmax-test --env-file podman.env -p 127.0.0.1:5050:5050 localhost/octopus-minmax:playwright
   podman logs -f octopus-minmax-test
   ```

   Open `http://localhost:5050`. Dry run tests the comparison and configuration;
   it does **not** exercise the website fallback. One-off mode leaves the dashboard
   running after the comparison. Stop it with `podman stop octopus-minmax-test`.

5. Validate the authenticated login and signup controls for Go, Agile and Cosy,
   stopping before Switch Tariff. The automated fixtures model the reported controls;
   they cannot prove the current live page structure or account eligibility.
6. Run one supervised live switch with `DRY_RUN=false`, confirm the fallback finds
   the correct enrolment and the API accepts the agreement, then check the resulting
   product on the account. Enable scheduled operation only after that succeeds.

For local tests outside the container, install requirements, run `python -m invisible_playwright fetch`, then
run `python -m unittest discover -s tests -v`. The full requirements target Python 3.12.

Note : Remove the --restart unless line if you set the ONE_OFF variable or it will continuously run.

#### Environment Variables

| Variable               | Description                                                                                                                                                                                                            |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ACC_NUMBER`           | Your Octopus Energy account number.                                                                                                                                                                                    |
| `API_KEY`              | API token for accessing your Octopus Energy account.                                                                                                                                                                   |
| `OCTOPUS_LOGIN_EMAIL`  | Octopus website email, required when API tariff initiation fails.                                                                                                                                                      |
| `OCTOPUS_LOGIN_PASSWD` | Octopus website password, required when API tariff initiation fails.                                                                                                                                                   |
| `TARIFFS`              | A list of tariffs to compare against. Default is go,agile,flexible                                                                                                                                                     |
| `EXECUTION_TIME`       | (Optional) The time (HH:MM) when the script should execute. Default is `23:30` (11 PM).                                                                                                                                |
| `SWITCH_THRESHOLD`     | A value (in pence) which the saving must be before the switch occurs. Default is `2` (2p).                                                                                                                             |
| `NOTIFICATION_URLS`    | (Optional) A comma-separated list of [Apprise](https://github.com/caronc/apprise) notification URLs for sending logs and updates. See [Apprise documentation](https://github.com/caronc/apprise/wiki) for URL formats. |
| `ONE_OFF`              | (Optional) A flag for you to simply trigger an immediate execution instead of starting scheduling.                                                                                                                     |
| `DRY_RUN`              | (Optional) A flag to compare but not switch tariffs.                                                                                                                                                                   |
| `TEST_PLAYWRIGHT`      | (Optional, default false) Test Go, Agile and Cosy website forms once at startup without submitting. Disables comparison and scheduling until restarted with this mode off.                                             |
| `BATCH_NOTIFICATIONS`  | (Optional) A flag to send messages in one batch rather than individually.                                                                                                                                              |
| `WEB_USERNAME`         | (Optional) Defaults to `admin`. Auth for the web dashboard.                                                                                                                                                            |
| `WEB_PASSWORD`         | (Optional) Defaults to `admin`. Auth for the web dashboard.                                                                                                                                                            |
| `WEB_PORT`             | (Optional) Defaults to `5050`.                                                                                                                                                                                         |

_Reminder: Change the password to something else other than default. It's not meant to be secure, it's just there to stop others on your network from accessing the dashboard and your API key. If they have access to your compose/config files you're already cooked._

#### Supported Tariffs

Below is a list of supported tariffs, their IDs (to use in environment variables), and whether they are switchable.

**None switchable tariffs are use for PRICE COMPARISON ONLY**

| Tariff Name          | Tariff ID  | Switchable |
| -------------------- | ---------- | ---------- |
| Flexible Octopus     | flexible   | ❌         |
| Agile Octopus        | agile      | ✅         |
| Cosy Octopus         | cosy       | ✅         |
| Octopus Go           | go         | ✅         |
| Octopus Go 12M Fixed | go-fix-12m | ✅         |

#### Setting up Apprise Notifications

The `NOTIFICATION_URLS` environment variable allows you to configure notifications using the powerful [Apprise](https://github.com/caronc/apprise) library. Apprise supports a wide variety of notification services, including Discord, Telegram, Slack, email, and many more.

To configure notifications:

1.  **Determine your desired notification services:** Decide which services you want to receive notifications on (e.g., Discord, Telegram).

2.  **Find the Apprise URL format for each service:** Consult the [Apprise documentation](https://github.com/caronc/apprise/wiki) to find the correct URL format for each service you've chosen. For example:
    - **Discord:** `discord://webhook_id/webhook_token`
    - **Telegram:** `tgram://bottoken/ChatID`

3.  **Set the `NOTIFICATION_URLS` environment variable:** Create a comma-separated string containing the Apprise URLs for all your desired services. For example:

    ```bash
    NOTIFICATION_URLS="discord://webhook_id/webhook_token,tgram://bottoken/ChatID,mailto://user:pass@example.com?to=recipient@example.com"
    ```

    Make sure to replace the example values with your actual credentials.

4.  **Restart the container (if using Docker) or run the script:** The bot will now send notifications to all the configured services.om"

    ```

    Make sure to replace the example values with your actual credentials.

    ```

5.  **Restart the container (if using Docker) or run the script:** The bot will now send notifications to all the configured services.
6.  **Restart the container (if using Docker) or run the script:** The bot will now send notifications to all the configured services.
