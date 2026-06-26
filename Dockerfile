# ==========================================================================
#  shortcap – Docker image for ephemeral subtitle rendering
#  Image: python:3.11-slim  |  FFmpeg + ImageMagick + MoviePy + Groq/Whisper
# ==========================================================================

# ---------- Stage: builder (dependencies only, for cache efficiency) ------
FROM python:3.11-slim AS builder

# System packages required by MoviePy / Pillow / FFmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        imagemagick \
        libmagick++-dev \
        fonts-dejavu-core \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# (No need to fix ImageMagick policy here — builder doesn't render text)

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the source tree and install the package in editable mode
COPY . .
RUN pip install --no-cache-dir -e .

# ---------- Stage: runtime ------------------------------------------------
FROM python:3.11-slim

# Reinstall only the runtime system libraries (no build headers)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        imagemagick \
        fonts-dejavu-core \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Fully open ImageMagick security policy so MoviePy's TextClip can use
# `convert` with @file reads, raster coders, and large allocations.
RUN POLICY_FILE=$(find /etc/ImageMagick* -name "policy.xml" 2>/dev/null | head -1) && \
    if [ -n "$POLICY_FILE" ]; then \
        echo '<?xml version="1.0" encoding="UTF-8"?>' > "$POLICY_FILE" && \
        echo '<!DOCTYPE policymap [<!ELEMENT policymap (policy)*><!ATTLIST policy domain CDATA #IMPLIED rights CDATA #IMPLIED pattern CDATA #IMPLIED>]>' >> "$POLICY_FILE" && \
        echo '<policymap>' >> "$POLICY_FILE" && \
        echo '  <policy domain="coder" rights="read|write" pattern="*" />' >> "$POLICY_FILE" && \
        echo '  <policy domain="path" rights="read|write" pattern="@*" />' >> "$POLICY_FILE" && \
        echo '</policymap>' >> "$POLICY_FILE" ; \
    fi

# Copy installed Python packages + app source from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

WORKDIR /data

# Default entry-point: the new wrapper
ENTRYPOINT ["python", "/app/app.py"]

# Allow overriding config path via CMD (defaults to /data/config.json)
CMD ["/data/config.json"]
