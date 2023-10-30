from __future__ import annotations
from functools import cached_property
import subprocess
from parsers import ap_names, get_parser
from generic_parser import TableParser
from audio_file import AudioFile
from loggers import ProcLogger, Logger, ProgressBar 
from variables import BIRDNET_AUDIO_DURATION, BIRDNET_SAMPLE_RATE, NOISE_LABEL
from segment import Segment
from argparse import ArgumentParser
from datetime import datetime
import pandas as pd
import json
import numpy as np
import fnmatch
import time
import re
import os


class LabelMapper:
    def __init__(self, label_settings_path: str, *args, **kwargs):
        try:
            with open(label_settings_path) as fp:
                self.json_obj = json.load(fp)
        except:
            self.json_obj = {}
        self.map_dict: dict = {} if not "map" in self.json_obj else self.json_obj["map"]
        self.whitelist = None if not "whitelist" in self.json_obj else self.json_obj["whitelist"]
        self.blacklist = None if not "blacklist" in self.json_obj else self.json_obj["blacklist"]
    
    def black_listed(self, label: str) -> bool:
        if self.whitelist:
            return label not in self.whitelist
        return label in self.blacklist
    
    def map(self, label: str) -> str:
        for k, v in self.map_dict.items():
            if re.match(k, label):
                label = v
                break
        if self.black_listed(label):
            label = "Noise"
        return label
    
class SegmentsWrapper:
    segments: list[Segment]
    audio_file: AudioFile | None

    def __init__(self, segments  = [], audio_file = None):
        self.segments = segments
        self.audio_file = audio_file

class BirdNetTrainer:
    tables_paths: list = []

    def __init__(
            self,
            tables_dir: str,
            table_format: str,
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

        if len(self.tables_paths) == 0:
            raise AttributeError("No annotations found in the provided folder.")

        self.audio_files: dict[str, SegmentsWrapper] = {}

        prog_bar = ProgressBar("Reading tables", len(self.tables_paths))
        for table_path in self.tables_paths:
            for rel_path, segment in zip(self.parser.get_audio_rel_no_ext_paths(table_path, self.tables_dir), 
                                         self.parser.get_segments(table_path)):
                self.audio_files.setdefault(rel_path, SegmentsWrapper()).segments.append(segment)
        for v in self.audio_files.values():
            v.segments = sorted(v.segments, key=lambda seg: seg.tstart)
        prog_bar.terminate()


    @property
    def n_tables(self):
        return len(self.tables_paths)
    
    @cached_property
    def n_segments(self):
        return sum([len(af_wrap.segments) for af_wrap in self.audio_files.values()])

    def extract_for_training(self, audio_files_dir: str, audio_file_ext: str, export_dir: str, logger: Logger, **kwargs) -> dict[str, str | int | float]:
        logger.print("Input annotations' folder:", self.tables_dir)
        logger.print("Input audio folder:", audio_files_dir)
        logger.print("Output audio folder:", export_dir)


        prog_bar = ProgressBar("Retrieving audio paths", self.n_segments)
        for rel_path, af_wrap in self.audio_files.items():
            path_no_ext = os.path.join(audio_files_dir, rel_path)
            audio_path = ".".join([path_no_ext, audio_file_ext])
            af_wrap.audio_file = AudioFile(audio_path)
            prog_bar.print(1)
        prog_bar.terminate()


        if "label_settings_path" in kwargs:
            prog_bar = ProgressBar("Changing labels", self.n_segments)
            label_mapper = LabelMapper(**kwargs)
            for af_wrap in self.audio_files.values():
                for seg in af_wrap.segments:
                    seg.label = label_mapper.map(seg.label)
                    prog_bar.print(1)
            prog_bar.terminate()



        prog_bar = ProgressBar("Exporting segments and noise", self.n_segments)
        proc_logger = ProcLogger(**kwargs)
        logger.print("Found", len(self.audio_files), "audio files.")
        for af_wrap in self.audio_files.values():
            af_wrap.audio_file.export_all_birdnet(export_dir, af_wrap.segments, proc_logger=proc_logger, logger=logger, progress_bar=prog_bar, **kwargs)
        prog_bar.terminate()

    def validate(self, other: BirdNetTrainer, *args, **kwargs):
        validate(ground_truth=self, to_validate=other, *args, **kwargs) 
    

def validate(ground_truth: BirdNetTrainer, to_validate: BirdNetTrainer, binary=False, positive_labels=None):

    all_rel_paths = set(ground_truth.audio_files.keys()) | set(to_validate.audio_files.keys())

    labels: set[str] = set()

    for bnt in [ground_truth, to_validate]:
        for af_wrapper in bnt.audio_files.values():
            for seg in af_wrapper.segments:
                labels.add(seg.label)
    
    labels.add(NOISE_LABEL)
    labels = sorted(labels)
    n_labels = len(labels)

    if binary and n_labels>2:
        if positive_labels is None:
            raise AttributeError("Binary classification with more than one label! Please specify the positive label(s).")
        if isinstance(positive_labels, str):
            positive_labels = [positive_labels]
        for bnt in [ground_truth, to_validate]:
            for af_wrapper in bnt.audio_files.values():
                for seg in af_wrapper.segments:
                    if seg.label not in positive_labels:
                        seg.label = NOISE_LABEL
                    else:
                        seg.label = "Positive"
        labels = ["Positive", NOISE_LABEL]
        n_labels = 2           
    
    index: dict[str, int] = {}

    for i, label in enumerate(labels):
        index[label] = i
    
    conf_time_matrix = np.zeros((n_labels, n_labels), np.float64)
    conf_count_matrix = np.zeros((n_labels, n_labels), np.float64)

    def set_conf_time(label_truth, label_prediction, duration):
        conf_time_matrix[index[label_truth], index[label_prediction]] += duration
    
    def set_both(label_truth, label_prediction, duration):
        set_conf_time(label_truth, label_prediction, duration)
        conf_count_matrix[index[label_truth], index[label_prediction]] += 1

               
    for rel_path in all_rel_paths:
        af_gt, af_tv = None, None
        if rel_path in ground_truth.audio_files:
            af_gt = ground_truth.audio_files[rel_path]

        if rel_path in to_validate.audio_files:
            af_tv = to_validate.audio_files[rel_path]

        if not rel_path in ground_truth.audio_files:
            for seg in af_tv.segments:
                if seg.label != NOISE_LABEL:
                    set_both(NOISE_LABEL, seg.label, seg.dur)
            continue
        
        if not rel_path in to_validate.audio_files:
            for seg in af_gt.segments:
                if seg.label != NOISE_LABEL:
                    set_both(seg.label, NOISE_LABEL, seg.dur)
            continue
    
        
        # if af_gt.audio_file is not None:
        #     duration = af_gt.audio_file.duration
        
        # elif af_tv.audio_file is not None:
        #     duration = af_tv.audio_file.duration


        segs_gt = Segment.get_intervaltree([s for s in af_gt.segments if s.label!=NOISE_LABEL])
        segs_tv = Segment.get_intervaltree([s for s in af_tv.segments if s.label!=NOISE_LABEL])



        for seg_gt in segs_gt:
            seg_gt = Segment.from_interval(seg_gt)
            overlapping = segs_tv[seg_gt.begin:seg_gt.end]
            if len(overlapping) == 0:
                set_both(seg_gt.label, NOISE_LABEL, seg_gt.dur)
                continue

            for seg_tv in overlapping:
                seg_tv = Segment.from_interval(seg_tv)
                set_both(seg_gt.label, seg_tv.label, seg_tv.overlapping_time(seg_gt))
            t = Segment.get_intervaltree(overlapping)
            t.merge_overlaps()
            tot_overlapping = sum([Segment.from_interval(s).overlapping_time(seg_gt) for s in t])
            set_conf_time(seg_gt.label, NOISE_LABEL, seg_gt.dur - tot_overlapping)

        for seg_tv in segs_tv:
            seg_tv = Segment.from_interval(seg_tv)
            overlapping = segs_gt[seg_tv.begin:seg_tv.end]
            if len(overlapping) == 0:
                set_both(NOISE_LABEL, seg_tv.label, seg_gt.dur)
                continue
            t = Segment.get_intervaltree(overlapping)
            t.merge_overlaps()
            tot_overlapping = sum([Segment.from_interval(s).overlapping_time(seg_gt) for s in t])
            set_conf_time(NOISE_LABEL, seg_tv.label, seg_tv.dur - tot_overlapping)
    
    def stats(matrix):
        precision = {}
        recall = {}
        f1score = {}
        for i, label in enumerate(labels):
            tp = matrix[i,i]
            mask = np.ones_like(matrix[i], np.bool_)
            mask[i] = 0
            fp = np.sum(matrix[:, i] * mask)
            fn = np.sum(matrix[i, :] * mask)
            p = 0 if tp==0 else tp / (tp + fp)
            r = 0 if tp==0 else tp / (tp + fn)
            precision[label] = p
            recall[label] = r
            f1score[label] =  0 if p==0 and r==0 else 2 * (p * r) / (p + r)
        df_matrix = pd.DataFrame(data=matrix, index=labels, columns=labels)
        df_metrics =  pd.DataFrame(
            {"precision": precision, "recall": recall, "f1 score": f1score},
            index=labels,
            columns=["precision","recall","f1 score"]
        )
        return df_matrix, df_metrics

    return  stats(conf_time_matrix), stats(conf_count_matrix)







if __name__ == "__main__":

    ts = time.time()
    arg_parser = ArgumentParser()
    arg_parser.description = f"Train and validate a custom BirdNet classifier based on given annotations by first exporting "\
                             f"{BIRDNET_AUDIO_DURATION.s}s segments."
    
    subparsers = arg_parser.add_subparsers(dest="action")

    """
        Parse arguments to extract audio chunks.
    """

    extract_parser = subparsers.add_parser("extract",
                                           help="Extracts audio chunks from long audio files using FFmpeg based on the given parser annotation." \
                                                "the result consists of multiple audio file, each 3s long \"chunk\", each in the corresponding." \
                                                "labelled folder, which can be used to train the BirdNet custom classifier.")
    
    extract_parser.add_argument("-i", "--input-dir",
                                dest="tables_dir",
                                help="Path to the folder of the (manual) annotations.",
                                default=".")
    
    extract_parser.add_argument("-re", "--recursive",
                                type=bool,
                                dest="recursive",
                                help="Wether to look for tables inside the root directory recursively or not (default=True).",
                                default=True)
    
    extract_parser.add_argument("-f", "--annotation-format",
                                dest="table_format",
                                choices=ap_names,
                                required=True,
                                help="Annotation format.")
    
    extract_parser.add_argument("-a", "--audio-root-dir",
                                dest="audio_files_dir",
                                help="Path to the root directory of the audio files (default=current working dir).", default=".")
    
    extract_parser.add_argument("-ie", "--audio-input-ext",
                                dest="audio_input_ext",
                                required=True,
                                help="Key-sensitive extension of the input audio files (default=wav).", default="wav")
    
    extract_parser.add_argument("-oe", "--audio-output-ext",
                                dest="audio_output_ext",
                                help="Key-sensitive extension of the output audio files (default=flac).", default="flac")
    

    extract_parser.add_argument("-o", "--output-dir",
                                dest="export_dir",
                                help="Path to the output directory. If doesn't exist, it will be created.",
                                default=".")
    
    extract_parser.add_argument("-r", "--resample",
                                dest="resample",
                                help=f"Resample the chunk to the given value in Hz. (default={BIRDNET_SAMPLE_RATE})",
                                type=int,
                                default=BIRDNET_SAMPLE_RATE)
    
    extract_parser.add_argument("-co", "--chunk-overlap",
                                dest="chunk_overlap",
                                help=f"Overlap in seconds between chunks for segments longer than {BIRDNET_AUDIO_DURATION.s}s."\
                                     F"If it is 0 (by default) the program may run faster.",
                                default=0)
    
    extract_parser.add_argument("-df", "--date-fromat",
                                dest="date_format",
                                help='Date format of the file. (default = "%%Y%%m%%d_%%H%%M%%S")',
                                type=str,
                                default="%Y%m%d_%H%M%S")
    
    """
        Parse arguments to train the model.
    """
    train_parser = subparsers.add_parser("train")



    """
        Parse arguments to validate BirdNet predictions.
    """
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("-gt", "--ground-truth",
                                dest="tables_dir_gt",
                                help="Path to the folder of the ground truth annotations (default=current working dir).",
                                default=".")
    
    validate_parser.add_argument("-tv", "--to-validate",
                            dest="tables_dir_tv",
                            help="Path to the folder of the annotations to validate (default=current working dir).",
                            default=".")
    
    validate_parser.add_argument("-fgt", "--annotation-format-ground-truth",
                                dest="table_format_gt",
                                required=True,
                                choices=ap_names,
                                help="Annotation format for ground truth data.")

    validate_parser.add_argument("-ftv", "--annotation-format-to-validate",
                                dest="table_format_tv",
                                choices=ap_names,
                                help="Annotation format for data to validate (default=raven).",
                                default="raven")
    
    validate_parser.add_argument("-o", "--output-dir",
                                dest="output_dir",
                                help="Path to the output directory (default=current working dir).",
                                default=".")
    
    validate_parser.add_argument("-re", "--recursive",
                                type=bool,
                                dest="recursive",
                                help="Wether to look for tables inside the root directory recursively or not (default=True).",
                                default=True)
    

    
    args, custom_args = arg_parser.parse_known_args()


    if args.action == "extract":
        os.makedirs(args.export_dir, exist_ok=True)
        export_dir = os.path.join(args.export_dir, datetime.utcnow().strftime("%Y%m%d_%H%M%SUTC"))
        os.mkdir(export_dir)
        
        # export_dir =args.export_dir
        

        logger = Logger(logfile_path=os.path.join(export_dir, "log.txt"))

        logger.print("Started processing...")
        ts = time.time()


        try:
            bnt = BirdNetTrainer(
                tables_dir=args.tables_dir,
                table_format=args.table_format,
                recursive_subfolders=args.recursive,
            )\
            .extract_for_training(
                audio_files_dir=args.audio_files_dir,
                audio_file_ext=args.audio_input_ext,
                export_dir=export_dir,
                audio_format=args.audio_output_ext,
                logger=logger,
                logfile_errors_path=os.path.join(export_dir, "error_log.txt"),
                logfile_success_path=os.path.join(export_dir, "success_log.txt"),
                label_settings_path = os.path.join(args.tables_dir, "labels.json"),
                resample=args.resample,
                overlap_s=args.chunk_overlap,
                date_format=args.date_format
            )
        
            logger.print(f"... end of processing (elapsed {time.time()-ts:.1f}s)")
        except Exception as e:
            print("An error occured and the operation was not completed!")
            print(f"Check {logger.logfile_path} for more information.")
            logger.print_exception(e)  
    elif args.action == "train":
        subprocess.run(["python3", "BirdNET-Analyzer/train.py"] + custom_args)
    elif args.action=="validate":
        print(args.tables_dir_gt)
        bnt_gt = BirdNetTrainer(
            tables_dir=args.tables_dir_gt,
            table_format=args.table_format_gt,
            recursive_subfolders=args.recursive

        )

        bnt_tv = BirdNetTrainer(
            tables_dir=args.tables_dir_tv,
            table_format=args.table_format_tv,
            recursive_subfolders=args.recursive
        )

        stats_time, stats_count =  validate(bnt_gt, bnt_tv)


        def save_stats(stats: tuple[pd.DataFrame, pd.DataFrame], suffix: str):
            fname = lambda f: os.path.join(args.output_dir, f"{f}_{suffix}.csv")
            df_matrix, df_metrics  = stats
            df_matrix.to_csv(fname("confusion_matrix"))
            df_metrics.to_csv(fname("validation_metrics"))
        
        save_stats(stats_time, "time")
        save_stats(stats_count, "count")

    

        


