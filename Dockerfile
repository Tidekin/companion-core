FROM postgres:16.15-trixie@sha256:1beaa3084eee2676f20a9cd9928f0bf2c5bf674f8e49f81017912183d377c61c AS postgres-client
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy HF_HUB_DISABLE_XET=1
WORKDIR /app
# Native backup clients and their shared libraries come from the matching, pinned Debian image.
COPY --from=postgres-client /usr/lib/postgresql/16/bin/pg_dump /usr/lib/postgresql/16/bin/pg_restore /usr/local/bin/
COPY --from=postgres-client /usr/lib/x86_64-linux-gnu/libpq.so.* \
    /usr/lib/x86_64-linux-gnu/libzstd.so.* \
    /usr/lib/x86_64-linux-gnu/liblz4.so.* \
    /usr/lib/x86_64-linux-gnu/libgssapi_krb5.so.* \
    /usr/lib/x86_64-linux-gnu/libldap.so.* \
    /usr/lib/x86_64-linux-gnu/libxxhash.so.* \
    /usr/lib/x86_64-linux-gnu/libkrb5.so.* \
    /usr/lib/x86_64-linux-gnu/libk5crypto.so.* \
    /usr/lib/x86_64-linux-gnu/libcom_err.so.* \
    /usr/lib/x86_64-linux-gnu/libkrb5support.so.* \
    /usr/lib/x86_64-linux-gnu/liblber.so.* \
    /usr/lib/x86_64-linux-gnu/libsasl2.so.* \
    /usr/lib/x86_64-linux-gnu/libkeyutils.so.* /usr/local/lib/postgresql/
COPY --from=postgres-client /usr/share/doc/ /usr/local/share/doc/postgresql-clients/
RUN printf '%s\n' /usr/local/lib/postgresql > /etc/ld.so.conf.d/postgresql-clients.conf && ldconfig
RUN pip install --no-cache-dir uv==0.12.10
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY companion ./companion
COPY companion_chat_client ./companion_chat_client
COPY source_reader ./source_reader
COPY companion_onebot_bridge ./companion_onebot_bridge
COPY companion_meme_protocol ./companion_meme_protocol
COPY companion_meme_sources ./companion_meme_sources
COPY companion_voice_node ./companion_voice_node
COPY companion_delivery_protocol ./companion_delivery_protocol
COPY migrations ./migrations
COPY db ./db
# Public source archives may have owner-only directories after private extraction.
RUN chmod -R a+rX companion companion_chat_client source_reader companion_onebot_bridge companion_meme_protocol companion_meme_sources companion_voice_node companion_delivery_protocol migrations db && \
    uv sync --frozen --no-dev && useradd -m -u 10001 companion && \
    mkdir -p /data /privacy && chown companion:companion /data /privacy
USER companion
ENTRYPOINT ["/app/.venv/bin/python", "-m", "companion"]
CMD ["serve", "--host", "0.0.0.0"]
