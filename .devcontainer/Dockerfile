FROM python:3.13

RUN --mount=type=bind,source=requirements.txt,target=/tmp/requirements.txt \
    pip3 install --no-cache-dir -r /tmp/requirements.txt

RUN --mount=type=bind,source=req_ha_default_config,target=/tmp/req_ha_default_config \
    pip3 install --no-cache-dir -r /tmp/req_ha_default_config

RUN wget https://github.com/AlexxIT/go2rtc/releases/download/v1.9.14/go2rtc_linux_amd64 -O /usr/local/bin/go2rtc \
    && chmod +x /usr/local/bin/go2rtc

RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg;
RUN echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null;
RUN apt update && apt install -y gh;

ARG USERNAME=vscode
ARG USER_UID=1000
ARG USER_GID=$USER_UID

RUN groupadd --gid $USER_GID $USERNAME \
    && useradd --uid $USER_UID --gid $USER_GID -ms /bin/bash $USERNAME

USER $USERNAME
