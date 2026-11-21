FROM python:3.13-slim
WORKDIR /bot
COPY arena.py bot.py combat.py mechfight.py policy.py ./
ARG BOT_POLICY=reset
ENV MECHFIGHT_POLICY=${BOT_POLICY}
USER 65534:65534
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
