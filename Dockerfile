# LC Waikiki RU Telegram Bot — Railway / container image.
#
# Uses the official Playwright Python image, which already ships Chromium and
# all the system libraries the browser needs (the hard part of running
# Playwright in a container). The image tag MUST match the playwright version
# pinned in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ensure the Chromium build matching this Playwright version is present.
RUN python -m playwright install chromium

# Copy the bot source.
COPY . .

# Long-polling Telegram bot — it dials out to Telegram, so no inbound port.
CMD ["python", "bot_tg.py"]
