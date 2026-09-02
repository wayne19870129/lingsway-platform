FROM ghcr.io/xtls/xray-core:26.3.27@sha256:592ec4d11f656db95598d01e76dbcc6e002d67360b96a5436500a938230f52c7 AS xray

FROM python:3.12-slim
ENV XRAY_LOCATION_ASSET=/usr/local/share/xray
WORKDIR /app
COPY --from=xray /usr/local/bin/xray /usr/local/bin/xray
COPY --from=xray /usr/local/share/xray/geoip.dat /usr/local/share/xray/geoip.dat
COPY --from=xray /usr/local/share/xray/geosite.dat /usr/local/share/xray/geosite.dat
COPY pyproject.toml /app/pyproject.toml
COPY backend/app /app/backend/app
RUN pip install --no-cache-dir .
RUN test -x /usr/local/bin/xray \
    && test -s /usr/local/share/xray/geoip.dat \
    && test -s /usr/local/share/xray/geosite.dat \
    && /usr/local/bin/xray version
CMD ["python", "-m", "backend.app.main"]
