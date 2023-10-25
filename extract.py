from parsers import available_parsers
from base_classes import TableParser, AudioFile
import numpy as np
import fnmatch
import time
import os


def get_parser(table_format: str, **parser_kwargs):
    table_format_name = table_format.lower()
    table_parser: TableParser = None
    for tp in available_parsers:
        tp_init: TableParser = tp(**parser_kwargs)
        if table_format_name in tp_init.names:
            table_parser = tp_init
    return table_parser

class BirdNetExtractor:
    tables_paths: list = []

    def __init__(
            self,
            table_format: str,
            tables_dir: str,
            recursive_subfolders = True,
            **parser_kwargs
        ):

        self.parser = get_parser(table_format, **parser_kwargs)
        self.tables_dir = tables_dir
        self.recursive_subfolders = recursive_subfolders
        if recursive_subfolders:
            for dirpath, dirs, files in os.walk(self.tables_dir): 
                for filename in fnmatch.filter(files, self.parser.table_fnmatch):
                    fpath = os.path.join(dirpath, filename)
                    self.tables_paths.append(fpath)
        else:
            for f in os.listdir(self.tables_dir):
                fpath = os.path.join(self.tables_dir, f)
                if os.path.isfile(fpath) and self.parser.is_table(fpath):
                    self.tables_paths.append(fpath)
        

    def extract_segments_from_table(self, table_path: str, audio_files_dir: str, audio_file_ext: str, export_dir: str, **kwargs):
        segments = self.parser.get_segments(table_path, **kwargs)
        audiofiles = self.parser.get_audio_files(table_path, audio_files_dir, audio_file_ext)

        for det, af in zip(segments, audiofiles):
            af.export_for_birdnet(det, export_dir, **kwargs)


    def extract_all_segments(self, audio_files_dir: str, audio_file_ext: str, export_dir: str,  **kwargs):
        for tp in self.tables_paths:
            self.extract_segments_from_table(tp, audio_files_dir, audio_file_ext, export_dir, **kwargs)


    def extract_noise_all_files(self, export_dir: str, **kwargs):
        for af in self.parser.all_audio_files.values():
            af.export_noise_birdnet(export_dir,**kwargs)


    def extract_for_training(self, audio_files_dir: str, audio_file_ext: str, export_dir: str,  **kwargs):
        self.extract_all_segments(audio_files_dir, audio_file_ext, export_dir, **kwargs)
        self.extract_noise_all_files(export_dir, **kwargs)

    def extract_for_training_efficient(self, audio_files_dir: str, audio_file_ext: str, export_dir: str, **kwargs):
        self.map_audiofile_segments: dict[AudioFile, list] = {}
        segments = []
        audiofiles = []
        for table_path in self.tables_paths:
            segments += self.parser.get_segments(table_path, **kwargs)
            audiofiles += self.parser.get_audio_files(table_path, audio_files_dir, audio_file_ext)

        for det, af in zip(segments, audiofiles):
            self.map_audiofile_segments.setdefault(af, []).append(det)

        with open("success.log", "w") as logfile_success:
            with open("test_err.log", "w") as logfile_errors:
                for af, dets in self.map_audiofile_segments.items():
                        af.export_all_birdnet(export_dir, dets,logfile_success=logfile_success, logfile_err=logfile_errors, **kwargs)





if __name__ == "__main__":

    ts = time.time()

    extr = BirdNetExtractor(
        "raven",
        "C:\\Users\\plaf\\Documents\\raven",
        False
    )

    extr.extract_for_training_efficient(
        audio_files_dir="C:\\Users\\plaf\\Music",
        audio_file_ext="wav",
        export_dir="C:\\Users\\plaf\\Documents\\raven\\out",
        audio_format="flac",
    )

    print(time.time() - ts)
    

