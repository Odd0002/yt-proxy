# syntax=docker/dockerfile:1
FROM python:3.10-slim
EXPOSE 80

WORKDIR /app
COPY . .

# Install utils needed for downloading and extracting ffmpeg
RUN apt-get update && apt-get install -y wget xz-utils

# Download ffmpeg needed for yt-dlp
RUN wget -qO - https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz | tar -xJf - 

# Move ffmpeg into the path used by the proxy
RUN mv ffmpeg-master-latest-linux64-gpl/bin ffmpeg-bin

# Install requirements
RUN pip install --no-cache-dir -r requirements.txt

# Add ffmpeg bin to the path
ENV PATH "$PATH:/app/ffmpeg-bin/"

# Actually run the app
CMD ["python", "/app/proxy.py"]