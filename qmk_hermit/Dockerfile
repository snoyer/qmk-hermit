FROM qmkfm/qmk_cli

# setup user so we don't have to run as root
ARG USER=qmkuser
ARG UID=1000
ARG GID=1000
RUN groupadd -g $GID -o $USER
RUN useradd -m -u $UID -g $GID -o -s /bin/bash $USER
USER $USER
WORKDIR /home/$USER


# get QMK
ARG QMK_GIT=https://github.com/qmk/qmk_firmware.git
ARG QMK_DIR=qmk_firmware
RUN git clone $QMK_GIT
WORKDIR $QMK_DIR

RUN make git-submodule
RUN python3 -m pip install --user qmk
RUN qmk setup

