ARG BUILD_FROM
FROM $BUILD_FROM

RUN echo "v-fresh-20260730-airbnb-ext"
RUN apk add --no-cache python3 py3-pip curl
RUN pip3 install --no-cache-dir --break-system-packages seam
COPY run.sh /
COPY shared_config.py /
COPY server.py /
COPY seam_sync.py /
COPY airbnb_extend.py /
COPY seam_log_trim.sh /
COPY dashboard.html /
COPY crontab /etc/crontabs/root
RUN chmod +x /run.sh /seam_log_trim.sh && chmod 0600 /etc/crontabs/root

CMD ["/run.sh"]
