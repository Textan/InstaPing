InstaPing: Real-Time Instagram Monitor

InstaPing is a lightweight Python-based automation tool designed to monitor Instagram activity without the need for the official Graph API. By utilizing Playwright for browser automation, it tracks new content from specific target accounts and monitors your direct message inbox, delivering instant push notifications directly to your iPhone via the Bark notification service.

🚀 Features

    Account Monitoring: Tracks new Posts, Reels, and Stories from a customizable list of public or followed accounts.
    
    DM Alerts: Monitors your primary inbox for unread direct messages and sends a notification including the sender's name.
    
    Persistent Sessions: Saves authentication states to a local JSON file to minimize login frequency and reduce the risk      of account flagging.

    State Management: Maintains a local record of "seen" content to ensure you only receive notifications for truly new        activity.

    Efficient Resource Usage: Automatically blocks heavy media assets (images/videos) during the scraping process to           reduce bandwidth and CPU overhead.

    Fail-Safe Mechanisms: Includes automated browser recovery and re-authentication logic if the monitoring cycle              encounters consecutive errors.
    
📦 Installation

    git clone https://github.com/Textan/InstaPing.git
    cd InstaPing

    pip install playwright requests

    playwright install chromium

⚙️ Configuration

    Open InstaPing.py and update the CONFIG section with your details:

    # ── CONFIG ────────────────────────────────────────────────────────────────────
    USERNAME          = "your_instagram_username"
    PASSWORD          = "your_instagram_password"
    BARK_TOKEN        = "your_bark_device_token"   
    ACCOUNTS_TO_WATCH = []  # Add Target Handles Here
    CHECK_INTERVAL    = 240                       # Seconds between Checks
    HEADLESS          = True                      # Set to False to Watch the Browser
    # ─────────────────────────────────────────────────────────────────────────────
