FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY bot.py email_fetcher.py backfill.py ./

# SERVICE=bot runs bot.py; SERVICE=fetcher runs email_fetcher.py
ENV SERVICE=bot

CMD ["sh", "-c", "if [ \"$SERVICE\" = 'fetcher' ]; then python email_fetcher.py; else python bot.py; fi"]
