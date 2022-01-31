import ffmpeg
import os
import asyncio
import yt_dlp
from urllib import parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass

hostname = "0.0.0.0"
server_port = 80
shared_dict: dict = dict()
ffmpeg_thread_count = "8"
ffmpeg_location = "ffmpeg-bin/ffmpeg"

@dataclass
class RequestOptions:
    video_id: str = None
    flip_video: bool = False
    video_speed: float = 1.0
    embed_subtitles: bool = False
    remove_sponsors: bool = True
    bitrate: str = "2M"
    preset: str = "faster"
    #hardware_encoding: bool = False

    def should_force_keyframes(self):
        '''Whether the original re-encode should have forced keyframes. Requires re-encoding the video, but is necessary for subtitles to display correctly when sponsorblock is used'''
        return self.remove_sponsors and self.embed_subtitles


class MyServer(BaseHTTPRequestHandler):
    request_info: RequestOptions = RequestOptions()
    
    def reset(self) -> None:
        '''Reset the client's state'''
        self.request_info = RequestOptions()

    def send_status_code(self, code: int) -> None:
        '''Send a status-code-only response, like a 500'''
        self.send_response(code)
        self.end_headers()
        self.wfile.write(bytes(0))
        return
    
    def do_GET(self):
        self.reset()
        asyncio.run(self.on_request())

    async def on_request(self):
        try:
            if (self.path == "/favicon.ico"):
                # don't have a favicon yet
                self.send_status_code(404)
                return
            
            if "index" in self.path:
                # don't have an index page yet
                self.send_status_code(404)
                return

            # set up request options
            self.handle_path(self.path)
            # TODO: implement range headers
            #self.handle_range_headers(???)

            # download the video
            await download_video(self.request_info)
            
            # serve the actual video data
            await self.serve_video()
            print("finished serving for ", self.client_address)
        
        except Exception as e:
            print("Error:", e)
            self.send_status_code(500)
            return
    
    def get_file_prefix(self) -> str:
        pass

    async def serve_video(self) -> None:
        video_id = self.request_info.video_id
        video_speed = self.request_info.video_speed
        if video_id + ".downloaded" not in shared_dict:
            self.send_status_code(404)
            return

        src_video_path = shared_dict[video_id + ".downloaded"]
        # TODO: refactor to use get_file_prefix
        lock_dict_name = '"' + video_id + "." + str(video_speed) + '"' + ".lock"
        output_file_name = '"' + video_id + "." + str(video_speed) + '"' + ".mp4"

        ffmpeg_opts = generate_ffmpeg_opts(self.request_info)

        # convert video if it is not already
        if output_file_name not in shared_dict:
            stream = ffmpeg.input(src_video_path)
            audio = stream.audio.filter_('atempo', str(video_speed))
            video = stream.video

            if has_subtitles(src_video_path):
                video = video.filter("subtitles", src_video_path)
            video = video.setpts(str(1/video_speed) + '*PTS')

            stream = ffmpeg.output(audio, video, output_file_name, **ffmpeg_opts)
            stream = ffmpeg.overwrite_output(stream)
            args = ffmpeg.compile(stream)

            new_bytes = bytearray()
            shared_dict[output_file_name] = new_bytes
            shared_dict[lock_dict_name] = None
            try:
                await get_mp4_bytes(args, output_file_name, new_bytes)
            except:
                print("Error getting mp4 bytes")
                self.send_status_code(500)
            await asyncio.sleep(0.1)
            del shared_dict[lock_dict_name]
            
        # read video data in and send it 250,000 bytes at a time
        encoded_data = shared_dict[output_file_name]

        # actually send the data

        # Send headers
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.end_headers()

        # Send data
        await self.send_bytes(lock_dict_name, encoded_data)

    async def send_bytes(self, lock_dict_name: str, data_to_send) -> None:
        start_index = 0
        read_data = 0
        while True:
            # send data 250kb at a time, for better 
            data = data_to_send[start_index:start_index + 250000]
            read_data = len(data)

            # if no data was read, and the file is not being generated, break since we finished sending the data
            if read_data == 0:
                if lock_dict_name not in shared_dict:
                    break
                await asyncio.sleep(0.1)
                continue

            self.wfile.write(data)
            start_index += len(data)
            await asyncio.sleep(0.1)
        
        # handle remaining data
        data = data_to_send[start_index:]
        read_data = len(data)
        self.wfile.write(data)
        start_index += len(data)


    # Handle setting the video id and settings from query parameters
    def handle_path(self, path):
        split_info = parse.urlsplit(path)

        self.request_info.video_id = split_info.path.strip('/').split(".")[0]
        query_info = parse.parse_qsl(split_info.query)
        for query_data in query_info:
            self.parse_query(query_data)

    # Handle a query string value
    def parse_query(self, query_entry: tuple[str, str]):
        key, value = query_entry
        
        # basically a switch statement
        if key.lower() == "speed":
            self.request_info.video_speed = float(value)
            return
        
        if key.lower() == "sponsorblock":
            self.request_info.remove_sponsors = parse_bool(value)
            return

        if key.lower() == "subtitles" or key.lower() == "subs":
            self.request_info.embed_subtitles = parse_bool(value)
            return

        if key.lower() == "bitrate" or key.lower() == "maxrate":
            self.request_info.bitrate = value
            return

        if key.lower() == "preset":
            self.request_info.bitrate = value
            return

        if key.lower() == "hwenc":
            self.request_info.hardware_encoding = parse_bool(value)
            return

        if key.lower() == "flip":
            self.request_info.flip_video = parse_bool(value)
            return

async def download_video(request_options: RequestOptions):
    video_id = request_options.video_id
    dict_video_name = video_id + ".downloaded"
    dl_lock_dict_name = video_id + ".download.lock"

    yt_dl_opts = generate_ytdl_opts(request_options)

    if dict_video_name not in shared_dict:
        shared_dict[dict_video_name] = None
        shared_dict[dl_lock_dict_name] = None
        url = "https://www.youtube.com/watch?v=" + video_id
        ydl = yt_dlp.YoutubeDL(yt_dl_opts)
        try:
            meta = ydl.extract_info(url, download=True)
        except Exception as e:
            print(e)
            del shared_dict[dict_video_name]
        else:
            video_id = meta["id"]
            video_ext = meta["ext"]
            shared_dict[dict_video_name] = f"{video_id}.{video_ext}"
        finally:
            await asyncio.sleep(0.1)
            del shared_dict[dl_lock_dict_name]
    
    while dl_lock_dict_name in shared_dict:
        # slow spinlock
        await asyncio.sleep(0.1)
    return

async def get_mp4_bytes(args, file_name: str, byte_array: bytearray):
    print("args:", args)
    process = await asyncio.create_subprocess_exec(*args)
    await process.wait()

    with open(file_name, 'rb') as file:
        data = file.read()
        byte_array.extend(data)

    os.remove(file_name)
    return


def generate_ytdl_opts(options: RequestOptions):
    opts = {
    "format": "bestvideo[height<=481]+bestaudio",
    "merge_output_format": "mp4",
    "outtmpl": f"%(id)s.%(ext)s",
    "noplaylist": True,
    "verbose": True,
    'overwrites': True,
    'ffmpeg_location': ffmpeg_location,
    'writesubtitles': options.embed_subtitles,
    'postprocessors': list()
    }

    if (options.remove_sponsors):
        opts["postprocessors"].append({'key': 'SponsorBlock'})
        opts["postprocessors"].append(
            {
                'key': 'ModifyChapters',
                'remove_sponsor_segments': ['sponsor', 'selfpromo', 'outro', 'interaction'],
                'force_keyframes': options.should_force_keyframes(),
            }
        )
    
    if (options.embed_subtitles):
        opts["postprocessors"].append({'key': 'FFmpegEmbedSubtitle'})
        
    return opts

def generate_ffmpeg_opts(options: RequestOptions):
    ffmpeg_opts = {
        "loglevel": "error",
        "preset":options.preset,
        "maxrate": options.bitrate,
        "bufsize": options.bitrate,
        "threads": ffmpeg_thread_count,
        "b:v": options.bitrate,
        "movflags": "+faststart",#"+empty_moov",
    }

    return ffmpeg_opts

def parse_bool(value: str):
    if (value.lower() == "false" or value == "0"):
        return False
    return True

def has_subtitles(video_path):
    for stream in ffmpeg.probe(video_path)["streams"]:
        if (stream["codec_type"] == "subtitle"):
            return True

    return False

def main():
    webServer = ThreadingHTTPServer((hostname, server_port), MyServer)
    print("Server started http://%s:%s" % (hostname, server_port))

    try:
        webServer.serve_forever()
    except KeyboardInterrupt:
        pass

    webServer.server_close()
    print("Server stopped.")

if __name__ == "__main__":
    main()
