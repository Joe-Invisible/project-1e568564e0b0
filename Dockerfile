FROM python:3.13-slim
WORKDIR /bot
COPY arena.py bot.py combat.py mechfight.py policy.py ./
ARG BOT_POLICY=bounded
ARG BOT_PROFILE=
ENV MECHFIGHT_POLICY=${BOT_POLICY}
ENV MECHFIGHT_PROFILE=${BOT_PROFILE}
USER 65534:65534
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
