# 独立出口代理（mihomo），只提供固定节点，不做任何订阅热更新或节点轮换
#
# 节点凭据不在镜像里：运行时由 compose 以只读方式挂载 config.yaml。
FROM alpine:3.20

ARG MIHOMO_VERSION=v1.19.31
# 默认校验的是 linux/arm64 版摘要；其它架构构建时必须传入对应的 sha256
ARG MIHOMO_SHA256=9e0f11afbf38426b8bd88fdc594678f8161c57eccb4e1b77acb12b493904f1d4
ARG TARGETARCH

RUN apk add --no-cache ca-certificates curl tzdata \
 && curl -fsSL --retry 3 --retry-delay 2 -o /tmp/mihomo.gz \
      "https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/mihomo-linux-${TARGETARCH}-${MIHOMO_VERSION}.gz" \
 && if [ -n "${MIHOMO_SHA256}" ]; then \
        echo "${MIHOMO_SHA256}  /tmp/mihomo.gz" | sha256sum -c - ; \
    else \
        echo "警告：未提供 MIHOMO_SHA256，跳过摘要校验" >&2 ; \
    fi \
 && gunzip -c /tmp/mihomo.gz > /usr/local/bin/mihomo \
 && chmod 0755 /usr/local/bin/mihomo \
 && rm -f /tmp/mihomo.gz

# 预置 geo 数据（GeoSite / GeoIP），避免运行时下载：容器里的下载会超时，
# 而且一旦文件损坏，GEOSITE 规则会让整个配置加载失败。
ARG GEO_RELEASE=latest
RUN set -eux; \
    for name in geosite.dat geoip.metadb; do \
        curl -fsSL --retry 3 --retry-delay 3 --max-time 300 -o "/tmp/${name}" \
            "https://github.com/MetaCubeX/meta-rules-dat/releases/download/${GEO_RELEASE}/${name}"; \
    done; \
    adduser -D -u 1000 -h /home/mihomo mihomo; \
    mkdir -p /home/mihomo/.config/mihomo; \
    mv /tmp/geosite.dat /home/mihomo/.config/mihomo/GeoSite.dat; \
    mv /tmp/geoip.metadb /home/mihomo/.config/mihomo/geoip.metadb; \
    chown -R mihomo:mihomo /home/mihomo; \
    ls -l /home/mihomo/.config/mihomo

USER 1000:1000
WORKDIR /home/mihomo/.config/mihomo

EXPOSE 7890 9090
ENTRYPOINT ["/usr/local/bin/mihomo", "-d", "/home/mihomo/.config/mihomo"]
