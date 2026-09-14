# Immutable release identity is public metadata, not a credential. Production
# builds pass both args from the signed full-stack release descriptor. Unknown
# defaults keep ordinary local builds working but make /v1/capabilities fail
# closed until an identity is deliberately supplied.
#
# These args are LINKED INTO the binary (see the build stage). The matching ENV
# lines in the runtime targets are a legacy fallback for images that embedded
# nothing: a container recreated against a cached image picks up a new
# environment while still running old code, so an env var can only ever assert an
# identity, never prove one. `docker run <image> version` reports both and flags
# a disagreement.
ARG DITTOBENCH_SOFTWARE_VERSION=unknown
ARG DITTOBENCH_SOURCE_SHA=unknown

# ---- build stage ----
FROM golang:1.26.6-alpine@sha256:af8d6740070b8906d12eae1c3e3ea0957fb63f492051ea05e354c38ef9fe88df AS build
WORKDIR /src/services/dittobench-api

# The AST structural fingerprint (internal/astfp) uses go-tree-sitter, which wraps
# the tree-sitter C parser via cgo — so the build needs a C toolchain. build-base
# pulls in gcc + musl-dev.
RUN apk add --no-cache build-base

# The scorer consumes the in-tree generator module. The build context is the
# monorepo root so a generator change and its scorer consumer are one reviewed
# source unit instead of a cross-repository version-bump PR.
COPY services/dittobench-api/go.mod services/dittobench-api/go.sum* ./
COPY research/dittobench-datagen/go.mod /src/research/dittobench-datagen/go.mod
RUN go mod download

COPY services/dittobench-api/ /src/services/dittobench-api/
COPY research/dittobench-datagen/ /src/research/dittobench-datagen/

# Stamp release identity INTO the binary. Declared here (not before COPY) so the
# dependency layers above stay cacheable across identity changes.
#
# The go toolchain's automatic vcs.revision stamping cannot serve this build: a
# git-URL build context arrives as a plain checkout with no .git, and this stage
# has no git binary, so the toolchain records nothing. -ldflags -X is therefore
# the authoritative mechanism; internal/release still reads build info first-class
# for host builds, where it does work.
ARG DITTOBENCH_SOFTWARE_VERSION
ARG DITTOBENCH_SOURCE_SHA
# cgo is required (tree-sitter); link everything statically against musl so the
# resulting binary carries no dynamic deps and still runs on distroless/static.
RUN CGO_ENABLED=1 GOOS=linux go build \
    -ldflags="-s -w -extldflags '-static' \
      -X 'github.com/ditto-assistant/dittobench-api/internal/release.sourceRevision=${DITTOBENCH_SOURCE_SHA}' \
      -X 'github.com/ditto-assistant/dittobench-api/internal/release.softwareVersion=${DITTOBENCH_SOFTWARE_VERSION}'" \
    -o /out/dittobench-api ./cmd/dittobench-api && \
    CGO_ENABLED=1 GOOS=linux go build \
    -ldflags="-s -w -linkmode external -extldflags '-static' \
      -X 'github.com/ditto-assistant/dittobench-api/internal/release.sourceRevision=${DITTOBENCH_SOURCE_SHA}' \
      -X 'github.com/ditto-assistant/dittobench-api/internal/release.softwareVersion=${DITTOBENCH_SOFTWARE_VERSION}'" \
    -o /out/dittobench-coding-executor-scorer ./cmd/dittobench-coding-executor-scorer && \
    CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" \
      -o /out/updater-bootstrap ./cmd/updater-bootstrap

# ---- immutable public confirmation assets ----
# The cleaned LongMemEval-S dataset is public benchmark input pinned to an
# immutable upstream revision and full content digest. It is not a credential.
# A clean BuildKit builder downloads and verifies these exact bytes. Keep the
# fetch inside a deterministic RUN layer instead of remote ADD: remote ADD asks
# the origin to revalidate on every build, while this layer can be restored from
# the architecture-specific release cache after its first verified download.
FROM alpine:3.22 AS confirmation-assets
RUN wget -q -O /longmemeval_s_cleaned.json \
      https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/98d7416c24c778c2fee6e6f3006e7a073259d48f/longmemeval_s_cleaned.json && \
    echo "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442  /longmemeval_s_cleaned.json" \
      | sha256sum -c - && \
    chmod 0444 /longmemeval_s_cleaned.json

# ---- public coding certification pack ----
# The default-off certification canary (DITTOBENCH_CODING_CANARY_ENABLED) loads
# a repo-shaped root. Copy the committed certification/v1 capsule and the locked
# inference policy its manifest names, verify every byte against these pins, and
# reject any other file or link. The manifest digest is the identity Platform
# binds into every certification lease, but the runtime loader does not re-hash
# the visible workspace or grader files, so this build-time check binds them.
FROM alpine:3.22 AS coding-certification-pack
COPY research/dittobench-coding-datagen/certification/v1/ \
    /pack/certification-root/research/dittobench-coding-datagen/certification/v1/
COPY packages/dittobench-coding-contract/testdata/coding_inference_policy_locked_v1.json \
    /pack/certification-root/packages/dittobench-coding-contract/testdata/coding_inference_policy_locked_v1.json
WORKDIR /pack/certification-root
RUN printf '%s\n' \
      "cb608113db0cc31001fe0a7294854453061f9e85d1471520100ce99eca97a903  research/dittobench-coding-datagen/certification/v1/manifest.json" \
      "7eb7029d0c80edbfba4bc3a26634edc48e191608902cb4906125ace7b66a1016  research/dittobench-coding-datagen/certification/v1/capsules/PRACTICE-LEDGER-001/visible/workspace/app.py" \
      "645b042006605024a9a166cd1e4a44302f0f09464a1c40b0370b74ea9319bf4a  research/dittobench-coding-datagen/certification/v1/capsules/PRACTICE-LEDGER-001/visible/workspace/tests/test_visible.py" \
      "e42313d1d3423c0e5164ddcd8c88a32058ee7dd7ef190fd9b6826a8a6a1b9bf8  research/dittobench-coding-datagen/certification/v1/capsules/PRACTICE-LEDGER-001/grader/tests/test_regression.py" \
      "6dd79225817b56ebf155f8344cd5faf752c8dd57802b21d6d2cbbae9cc2ff0b4  packages/dittobench-coding-contract/testdata/coding_inference_policy_locked_v1.json" \
      > /tmp/certification-pack.sha256 && \
    sha256sum -c /tmp/certification-pack.sha256 && \
    cut -c67- /tmp/certification-pack.sha256 | sort > /tmp/certification-pack.expected && \
    find . -type f | sed 's|^\./||' | sort > /tmp/certification-pack.actual && \
    diff -u /tmp/certification-pack.expected /tmp/certification-pack.actual && \
    test -z "$(find . ! -type f ! -type d)" && \
    find . -type d -exec chmod 0555 {} + && \
    find . -type f -exec chmod 0444 {} +

# ---- validator sandbox runtime ----
# This opt-in target runs the API beside a host Docker daemon. The binary shells
# out to both git and docker when it materializes and runs miner submissions.
# Keep the ordinary runtime below as the default target for hosted practice.
FROM docker:28.3.3-cli-alpine3.22 AS sandbox
ARG DITTOBENCH_SOFTWARE_VERSION
ARG DITTOBENCH_SOURCE_SHA
RUN apk add --no-cache git
COPY --from=build /out/dittobench-api /dittobench-api
COPY --from=build /out/updater-bootstrap /updater-bootstrap
COPY --chmod=0555 scripts/validator-stack-auto-update.sh \
    /opt/ditto/validator-stack-auto-update.sh
COPY --chown=65532:65532 \
    packages/ditto-screening-protocol/ditto_screening_protocol/data/ \
    /opt/ditto/confirmation/
RUN chmod 0555 /opt/ditto/confirmation && \
    find /opt/ditto/confirmation -type f -exec chmod 0444 {} +
COPY --from=confirmation-assets --chown=65532:65532 --chmod=0444 \
    /longmemeval_s_cleaned.json \
    /opt/ditto/confirmation/longmemeval_s_cleaned.json
COPY --chown=65532:65532 --chmod=0444 \
    packages/dittobench-coding-contract/testdata/coding_inference_policy_locked_v1.json \
    /opt/ditto/coding/coding_inference_policy_locked_v1.json
# BuildKit applies --chmod=0444 to the implicitly created /opt/ditto/coding, which
# leaves the non-root scorer unable to traverse to the locked policy. Keep the
# directory traversable but read-only. The verified certification pack is
# root-owned, 0555/0444, and consulted only when the canary gate is enabled.
COPY --from=coding-certification-pack /pack/ /opt/ditto/coding/
RUN chmod 0555 /opt/ditto/coding
ENV HOME=/tmp
# Legacy fallback only; the binary above already carries these values.
ENV DITTOBENCH_SOFTWARE_VERSION=${DITTOBENCH_SOFTWARE_VERSION}
ENV DITTOBENCH_SOURCE_SHA=${DITTOBENCH_SOURCE_SHA}
USER 65532:65532
EXPOSE 8000
ENTRYPOINT ["/updater-bootstrap"]

# ---- runtime stage ----
FROM gcr.io/distroless/static-debian12:nonroot AS runtime
ARG DITTOBENCH_SOFTWARE_VERSION
ARG DITTOBENCH_SOURCE_SHA
COPY --from=build /out/dittobench-api /dittobench-api
# Legacy fallback only; the binary above already carries these values.
ENV DITTOBENCH_SOFTWARE_VERSION=${DITTOBENCH_SOFTWARE_VERSION}
ENV DITTOBENCH_SOURCE_SHA=${DITTOBENCH_SOURCE_SHA}
EXPOSE 8000
ENTRYPOINT ["/dittobench-api", "-port", "8000"]

# ---- dedicated coding executor scorer artifact ----
# This target is intentionally not the normal DittoBench scorer. It contains the
# Docker CLI needed to control only the dedicated rootless daemon and the locked
# policy required by its candidate-capability router. A later reviewed service
# profile must still supply the socket, token, private state, host network, and
# narrowed firewall before it can receive work.
FROM docker:28.3.3-cli-alpine3.22 AS coding-executor-scorer
ARG DITTOBENCH_SOFTWARE_VERSION
ARG DITTOBENCH_SOURCE_SHA
LABEL io.heyditto.dittobench.coding-executor-scorer-contract="1"
LABEL io.heyditto.dittobench.coding-executor-locked-policy-sha256="b2f38d9f6b5484e9a056d74be4dc0250912f05c9e51512801b590dff934a41d6"
LABEL org.opencontainers.image.revision="${DITTOBENCH_SOURCE_SHA}"
COPY --from=build /out/dittobench-coding-executor-scorer /dittobench-coding-executor-scorer
COPY --chown=65532:65532 \
    packages/dittobench-coding-contract/testdata/coding_inference_policy_locked_v1.json \
    /opt/ditto/coding/coding_inference_policy_locked_v1.json
RUN chmod 0555 /opt/ditto /opt/ditto/coding && \
    chmod 0444 /opt/ditto/coding/coding_inference_policy_locked_v1.json
ENV HOME=/tmp
ENV DITTOBENCH_SOFTWARE_VERSION=${DITTOBENCH_SOFTWARE_VERSION}
ENV DITTOBENCH_SOURCE_SHA=${DITTOBENCH_SOURCE_SHA}
USER 65532:65532
ENTRYPOINT ["/dittobench-coding-executor-scorer"]
CMD []
