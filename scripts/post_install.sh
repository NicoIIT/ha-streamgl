WEBRTC_VERSION=3.6.1
TMP_TAR_FILE=/tmp/webrtc
TMP_TAR_DIR=/tmp/webrtc_dir
INSTALL_DIR=/workspaces/ha-streamgl/config/custom_components/webrtc
LINK_DIR=/workspaces/ha-streamgl/custom_components/webrtc

wget "https://github.com/AlexxIT/WebRTC/archive/refs/tags/v${WEBRTC_VERSION}.tar.gz" -O ${TMP_TAR_FILE}

mkdir -p ${TMP_TAR_DIR}
tar -xzf ${TMP_TAR_FILE} -C ${TMP_TAR_DIR}

rm -rf ${INSTALL_DIR}
mkdir -p ${INSTALL_DIR}
mv ${TMP_TAR_DIR}/WebRTC-${WEBRTC_VERSION}/custom_components/webrtc/* ${INSTALL_DIR}
rm -f ${LINK_DIR}
ln -s ${INSTALL_DIR} ${LINK_DIR}

rm -f ${TMP_TAR_FILE}
rm -rf ${TMP_TAR_DIR}
