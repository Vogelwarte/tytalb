from __future__ import annotations
import warnings
from copy import deepcopy
from functools import cached_property
import subprocess
from parsers import ap_names, get_parser
from generic_parser import TableParser
from audio_file import AudioFile
from loggers import ProcLogger, Logger, ProgressBar 
from variables import BIRDNET_AUDIO_DURATION, BIRDNET_SAMPLE_RATE, NOISE_LABEL, AUDIO_EXTENSION_PRIORITY
from segment import Segment
from argparse import ArgumentParser, BooleanOptionalAction
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
        return label in self.blacklist if self.blacklist else False
    
    def map(self, label: str) -> str:
        for k, v in self.map_dict.items():
            if re.match(k, label):
                label = v
                break
        if self.black_listed(label):
            label = "Noise"
        return label
    
class SegmentsWrapper:
    def __init__(self, unique = True, segments: list[Segment] | None  = None, audio_file: AudioFile | None = None):
        if segments is None:
            segments = []
        self.unique = unique
        self.segments: list[Segment] = segments
        self.audio_file: AudioFile | None = audio_file
        

class Annotations:
    def __init__(
            self,
            tables_dir: str,
            table_format: str,
            logger: Logger,
            recursive_subfolders = True,
            **parser_kwargs
        ):

        self.parser = get_parser(table_format, **parser_kwargs)
        self.tables_dir = tables_dir
        self.recursive_subfolders = recursive_subfolders
        self.tables_paths: list = []

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

        self.audio_files: dict[str, SegmentsWrapper] = dict()
        prog_bar = ProgressBar("Reading tables", len(self.tables_paths))
        for table_path in self.tables_paths:
            for rel_path, segment in zip(self.parser.get_audio_rel_no_ext_paths(table_path, self.tables_dir), 
                                         self.parser.get_segments(table_path)):
                basename = os.path.basename(rel_path)
                unique = True
                if rel_path in self.audio_files.keys():
                    self.audio_files[rel_path].segments.append(segment)
                    continue
                    
                unique = True
                for rp in self.audio_files.keys():
                    bnm = os.path.basename(rp)
                    if basename == bnm:
                        logger.print(f"Found two files with the same name: {basename} ({rp} and {rel_path}). "
                                      "The output files will contain the path to ensure uniquess.")
                        unique=False
                        break
                self.audio_files.setdefault(rel_path, SegmentsWrapper(unique)).segments.append(segment)
                
        for v in self.audio_files.values():
            v.segments = sorted(v.segments, key=lambda seg: seg.tstart)
        prog_bar.terminate()



    @property
    def n_tables(self):
        return len(self.tables_paths)
    
    @cached_property
    def n_segments(self):
        return sum([len(af_wrap.segments) for af_wrap in self.audio_files.values()])

    def extract_for_training(self, audio_files_dir: str, export_dir: str, logger: Logger, include_path=False, **kwargs):
        """
        Extract BIRDNET_AUDIO_DURATION-long chunks to train a custom classifier.
        """
        logger.print("Input annotations' folder:", self.tables_dir)
        logger.print("Input audio folder:", audio_files_dir)
        logger.print("Output audio folder:", export_dir)

        audiodir_files = [f for f in os.listdir(audio_files_dir) if os.path.isfile(os.path.join(audio_files_dir,f))
                                                                 and f.split(".")[-1].lower() in AUDIO_EXTENSION_PRIORITY]
        prog_bar = ProgressBar("Retrieving audio paths", self.n_segments)
        for rel_path, af_wrap in self.audio_files.items():
            # Get all files starting with this filename
            audio_candidates = fnmatch.filter(audiodir_files, f"{rel_path}.*")
            if not audio_candidates:
                raise Exception(f"No audio files found starting with relative path "\
                                f"{rel_path} and extension {'|'.join(AUDIO_EXTENSION_PRIORITY)} "\
                                f"inside {audio_files_dir}.")
            # Give the priority based on `AUDIO_EXTENSION_PRIORITY`
            priority = lambda fname: AUDIO_EXTENSION_PRIORITY.index(fname.split(".")[-1].lower())
            chosen_audio_rel_path = min(audio_candidates, key = priority)

            audio_path = os.path.join(audio_files_dir, chosen_audio_rel_path)
            af = AudioFile(audio_path)
            af.set_date(**kwargs)
            if include_path or not af_wrap.unique:
                # If the filename is not unique (or the user decides to) include  
                # the relative path in the output filename.
                path = os.path.normpath(os.path.dirname(chosen_audio_rel_path))
                splits = path.split(os.sep)
                af.prefix = f"{'_'.join(splits)}_{af.prefix}"           
            af_wrap.audio_file = af
            prog_bar.print(1)
        prog_bar.terminate()


        if "label_settings_path" in kwargs and os.path.isfile(kwargs["label_settings_path"]):
            prog_bar = ProgressBar("Changing labels", self.n_segments)
            label_mapper = LabelMapper(**kwargs)
            for af_wrap in self.audio_files.values():
                for seg in af_wrap.segments:
                    seg.label = label_mapper.map(seg.label)
                    prog_bar.print(1)
            prog_bar.terminate()

        prog_bar = ProgressBar("Exporting segments", self.n_segments)
        proc_logger = ProcLogger(**kwargs)
        logger.print("Found", len(self.audio_files), "audio files.")
        for af_wrap in self.audio_files.values():
            af_wrap.audio_file.export_all_birdnet(export_dir, af_wrap.segments, proc_logger=proc_logger, logger=logger, progress_bar=prog_bar, **kwargs)
        prog_bar.terminate()

    def filter_confidence(self, confidence_threshold: float):
        copy = deepcopy(self)

        


    def validate(self, other: Annotations, *args, **kwargs):
        validate(ground_truth=self, to_validate=other, *args, **kwargs) 
    

def validate(
        ground_truth: Annotations,
        to_validate: Annotations,
        binary=False,
        positive_labels: str=None,
        late_start = False,
        early_stop = False 
        ):
    """
    Compare the annotations to validate to the ground truth ones.
    Returns a tuple of shape (2,2). The first element of the tuple
    contains the confusion matrix and the metrics (as tuple) 
    in terms of the count of overlapping segments, while the second 
    in terms of the overlapping time.

    Problems
    --------
     - If ground-truth annotation times are overlapping, in the 
       matrix they will appear multiple times, so some annotations
       can be considered wrong although correct.
     - The true negatives (noise matching noise) are not registered
       in the matrix.
    """

    all_rel_paths = set(ground_truth.audio_files.keys()) | set(to_validate.audio_files.keys())
    labels: set[str] = set()

    # Union the labels in ground truth and set to validate
    for bnt in [ground_truth, to_validate]:
        for af_wrapper in bnt.audio_files.values():
            for seg in af_wrapper.segments:
                labels.add(seg.label)


    
    labels.add(NOISE_LABEL)
    labels = sorted(labels)
    n_labels = len(labels)

    if binary and n_labels>2:
        ground_truth = deepcopy(ground_truth)
        to_validate = deepcopy(to_validate)
        if positive_labels is None:
            raise AttributeError("Binary classification with more than one label! Please specify the positive label(s).")
        if isinstance(positive_labels, str):
            positive_labels = [positive_labels]
        found = False
        for bnt in [ground_truth, to_validate]:
            for af_wrapper in bnt.audio_files.values():
                for seg in af_wrapper.segments:
                    # Label the positive segments as "positive" and the other with `NOISE_LABEL`
                    if seg.label not in positive_labels:
                        seg.label = NOISE_LABEL
                    else:
                        seg.label = "Positive"
                        found = True
        if not found:
            warnings.warn(f"No label from \"{','.join(positive_labels)}\" found.")
        labels = ["Positive", NOISE_LABEL]
        n_labels = len(labels)          
    
    index: dict[str, int] = {}

    # Map label to the row/column of the confusion matrix
    for i, label in enumerate(labels):
        index[label] = i
    
    # Confusion matrices for time and count
    conf_time_matrix = np.zeros((n_labels, n_labels), np.float64)
    conf_count_matrix = np.zeros((n_labels, n_labels), np.float64)

    # Shortcuts for setting the confusion matrices
    def set_conf_time(label_truth, label_prediction, duration):
        conf_time_matrix[index[label_truth], index[label_prediction]] += duration
    
    def set_both(label_truth, label_prediction, duration):
        set_conf_time(label_truth, label_prediction, duration)
        # If there is some overlap, we add one to the count
        conf_count_matrix[index[label_truth], index[label_prediction]] += 1

               
    for rel_path in all_rel_paths:
        af_gt, af_tv = None, None
        # Get the audio file for the ground truth
        if rel_path in ground_truth.audio_files:
            af_gt = ground_truth.audio_files[rel_path]

        # Get the audio file for the validation
        if rel_path in to_validate.audio_files:
            af_tv = to_validate.audio_files[rel_path]

        # If there are no labels in the ground truth, there are only FP
        if not rel_path in ground_truth.audio_files:
            for seg in af_tv.segments:
                if seg.label != NOISE_LABEL:
                    set_both(NOISE_LABEL, seg.label, seg.dur)
            continue
        
        # If there are no labels in the validation, there are only FN
        if not rel_path in to_validate.audio_files:
            for seg in af_gt.segments:
                if seg.label != NOISE_LABEL:
                    set_both(seg.label, NOISE_LABEL, seg.dur)
            continue

        min_tstart = min([s.tstart for s in af_gt.segments])
        max_tend = max([s.tend for s in af_gt.segments])

        def interval_tree(af: SegmentsWrapper):
            # If late_start and early_stop, restrict the interval
            return Segment.get_intervaltree([s for s in af.segments if s.label!=NOISE_LABEL 
                                             and (not late_start or s.tend >= min_tstart)
                                             and (not early_stop or s.tstart <= max_tend)])
        
        segs_gt = interval_tree(af_gt)
        segs_tv = interval_tree(af_tv)
    


        for seg_gt in segs_gt:
            seg_gt = Segment.from_interval(seg_gt)

            overlapping = segs_tv[seg_gt.begin:seg_gt.end]

            if len(overlapping) == 0:
                # The ground truth has no label for this interval, therefore FP
                set_both(seg_gt.label, NOISE_LABEL, seg_gt.dur)
                continue

            for seg_tv in overlapping:
                # If overlapping set the confusion matrices accordingly
                seg_tv = Segment.from_interval(seg_tv)
                set_both(seg_gt.label, seg_tv.label, seg_tv.overlapping_time(seg_gt))
            
            # Set all the time in which no overlap was found as FP
            t = Segment.get_intervaltree(overlapping)
            t.merge_overlaps()
            tot_overlapping = sum([Segment.from_interval(s).overlapping_time(seg_gt) for s in t])
            set_conf_time(seg_gt.label, NOISE_LABEL, seg_gt.dur - tot_overlapping)

        for seg_tv in segs_tv:
            seg_tv = Segment.from_interval(seg_tv)
            overlapping = segs_gt[seg_tv.begin:seg_tv.end]
            if len(overlapping) == 0:
                # The annotation to validate have no label for this interval, therefore FN
                set_both(NOISE_LABEL, seg_tv.label, seg_gt.dur)
                continue

            # Set all the time in which no overlap was found as FP
            t = Segment.get_intervaltree(overlapping)
            t.merge_overlaps()
            tot_overlapping = sum([Segment.from_interval(s).overlapping_time(seg_gt) for s in t])
            set_conf_time(NOISE_LABEL, seg_tv.label, seg_tv.dur - tot_overlapping)

        # Note that TN is never set (therefore 0)
        # TODO: Maybe implement this?
    
    def stats(matrix):
        precision = {}
        recall = {}
        f1score = {}
        false_positive = {}
        false_negative = {}
        for i, label in enumerate(labels):
            tp = matrix[i,i]
            mask = np.ones_like(matrix[i], np.bool_)
            mask[i] = 0
            fp = np.dot(matrix[:, i], mask)
            fn = np.dot(matrix[i, :], mask)
            p = 0 if tp==0 else tp / (tp + fp)
            r = 0 if tp==0 else tp / (tp + fn)
            false_positive[label] = fp
            false_negative[label] = fn
            precision[label] = p
            recall[label] = r
            f1score[label] =  0 if p==0 and r==0 else 2 * (p * r) / (p + r)
        df_matrix = pd.DataFrame(data=matrix, index=labels, columns=labels)
        df_matrix.index.name = "True\\Prediction"
        data = {
            "precision": precision,
            "recall": recall,
            "f1 score": f1score,
            "false posit ive": false_positive,
            "false negative": false_negative
        }
        
        df_metrics =  pd.DataFrame(
            data,
            index=labels,
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
                                           help="Extracts audio chunks from long audio files using FFmpeg based on the given parser annotation. " \
                                                'The result consists of multiple audio files ("chunks"), each 3s long, placed in the corresponding ' \
                                                "labelled folder, which can be used to train the BirdNet custom classifier.")
    
    extract_parser.add_argument("-i", "--input-dir",
                                dest="tables_dir",
                                help="Path to the folder of the (manual) annotations.",
                                default=".")
    
    extract_parser.add_argument("-re", "--recursive",
                                type=bool,
                                dest="recursive",
                                help="Wether to look for tables inside the root directory recursively or not (default=True).",
                                default=True,
                                action=BooleanOptionalAction)
    
    extract_parser.add_argument("-f", "--annotation-format",
                                dest="table_format",
                                choices=ap_names,
                                required=True,
                                help="Annotation format.")
    
    extract_parser.add_argument("--header",
                                dest="header",
                                help="Whether the annotation tables have an header. The default value is defined "\
                                     "by the annotations parser.",
                                action=BooleanOptionalAction)
    
    
    extract_parser.add_argument("-a", "--audio-root-dir",
                                dest="audio_files_dir",
                                help="Path to the root directory of the audio files (default=current working dir).", default=".")
    
    extract_parser.add_argument("-oe", "--audio-output-ext",
                                dest="audio_output_ext",
                                help="Key-sensitive extension of the output audio files (default=flac).", default="flac")
    

    extract_parser.add_argument("-o", "--output-dir",
                                dest="export_dir",
                                help="Path to the output directory. If doesn't exist, it will be created.",
                                default=".")

    extract_parser.add_argument("--tstamp-subdir",
                                dest="tstamp_outdir",
                                help="Whether to create an output subfolder with the current timestamp which "\
                                     "which will contain the output files.",
                                default=True,
                                action=BooleanOptionalAction)

    extract_parser.add_argument("-l", "--label-settings",
                                dest="label_settings_path",
                                help="Path to the file used to map and filter labels. Please refer to `README.md`. "\
                                     "By default the file is `labels.json` in the root directory of annotations.",
                                type=str,
                                default=None)
    
    extract_parser.add_argument("-r", "--resample",
                                dest="resample",
                                help=f"Resample the chunk to the given value in Hz. (default={BIRDNET_SAMPLE_RATE})",
                                type=int,
                                default=BIRDNET_SAMPLE_RATE)
    
    extract_parser.add_argument("-co", "--chunk-overlap",
                                dest="chunk_overlap",
                                help=f"Overlap in seconds between chunks for segments longer than {BIRDNET_AUDIO_DURATION.s}s. "\
                                     F"If it is 0 (by default) the program may run faster.",
                                default=0)
    
    extract_parser.add_argument("-df", "--date-format",
                                dest="date_format",
                                help='Date format of the file. (default = "%%Y%%m%%d_%%H%%M%%S")',
                                type=str,
                                default="%Y%m%d_%H%M%S")
    
    extract_parser.add_argument("-ls", "--late-start",
                                dest="late_start",
                                help='Whether to not consider the interval between the start of the recording and the first '\
                                     'annotation (default = False)',
                                type=bool,
                                action=BooleanOptionalAction,
                                default=False)
    
    extract_parser.add_argument("-es", "--early-stop",
                                dest="early_stop",
                                help='Whether to not consider the interval between the last annotation '\
                                     'and the end of the recording (default = False)',
                                type=bool,
                                action=BooleanOptionalAction,
                                default=False)
    
    # extract_parser.add_argument("-d", "--debug",
    #                             dest="debug",
    #                             help='Whether to log debug informations too.',
    #                             type=bool,
    #                             action=BooleanOptionalAction,
    #                             default=False)

    
    """
        Parse arguments to train the model.
    """
    train_parser = subparsers.add_parser("train", help="Train a custom classifier using BirdNet Analyzer. "\
                                                       "The args are passed directly to `train.py` from BirdNet.")



    """
        Parse arguments to validate BirdNet predictions.
    """
    validate_parser = subparsers.add_parser("validate", help="Validate the output from BirdNet Analyzer with some ground truth annotations. "\
                                                             "This creates two confusion matrices: one for the time (`confusion_matrix_time.csv`) "\
                                                             "and one for the count (`confusion_matrix_count.csv`)"\
                                                             "of (in)correctly identified segments of audio. From this, recall, precision and "\
                                                             "f1 score are computed and output in different tables (`validation_metrics_count.csv` "\
                                                             "and `validation_metrics_time.csv`).")

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
                                default="birdnet_raven")
    
    validate_parser.add_argument("-o", "--output-dir",
                                dest="output_dir",
                                help="Path to the output directory (default=current working dir).",
                                default=".")
    
    validate_parser.add_argument("-re", "--recursive",
                                dest="recursive",
                                help="Wether to look for tables inside the root directory recursively or not (default=True).",
                                type=bool,
                                action=BooleanOptionalAction,
                                default=True)

    validate_parser.add_argument("-ls", "--late-start",
                                dest="late_start",
                                help='Whether to not consider the interval between the start of the ground truth recording '\
                                     'and the first annotation (default = False)',
                                type=bool,
                                action=BooleanOptionalAction,
                                default=False)
    
    validate_parser.add_argument("-es", "--early-stop",
                                dest="early_stop",
                                help='Whether to not consider the interval between the last annotation '\
                                        'and the end of the recording (default = False)',
                                type=bool,
                                action=BooleanOptionalAction,
                                default=False)
    
    validate_parser.add_argument("-b", "--binary",
                                dest="binary",
                                help='Whether to validate as binary classification. If set, and '\
                                     'the POSITIVE_LABEL is not provided, an exception will be raised.',
                                type=bool,
                                action=BooleanOptionalAction,
                                default=False)
    
    validate_parser.add_argument("-p", "--positive-labels",
                                dest="positive_labels",
                                help='Comma-separated labels considered as positive for the binary classification.',
                                type=str)
    
    validate_parser.add_argument("-cts", "--conf-thresholds-start",
                                 dest="confidence_thresholds_start",
                                 help="Start range for confidence thresholds.",
                                 type=float,
                                 default=0)
    
    validate_parser.add_argument("-cte", "--conf-thresholds-end",
                                 dest="confidence_thresholds_end",
                                 help="End range for confidence thresholds",
                                 type=float,
                                 default=1)
    
    validate_parser.add_argument("-ct", "--conf-thresholds",
                                 dest="confidence_thresholds",
                                 help="Number of thresholds to filter the data to validate "\
                                      "(linearly distributed between CONFIDENCE_THRESHOLDS_START and "\
                                      "CONFIDENCE_THRESHOLDS_END). The table format must have a field "\
                                      "for the confidence and it has to be defined in the parser.",
                                 type=int,
                                 default=None)

    
    args, custom_args = arg_parser.parse_known_args()


    if args.action == "extract":
        os.makedirs(args.export_dir, exist_ok=True)
        export_dir =args.export_dir       
        if args.tstamp_outdir:
            export_dir = os.path.join(args.export_dir, datetime.utcnow().strftime("%Y%m%d_%H%M%SUTC"))
            os.mkdir(export_dir)
        

        logger = Logger(logfile_path=os.path.join(export_dir, "log.txt"))

        logger.print(args)

        logger.print("Started processing...")
        ts = time.time()

        label_settings_path = args.label_settings_path
        if label_settings_path is None:
            label_settings_path = os.path.join(args.tables_dir, "labels.json")

        parser_kwargs = {}
        if args.header is not None:
            parser_kwargs["header"] = args.header


        try:
            bnt = Annotations(
                tables_dir=args.tables_dir,
                table_format=args.table_format,
                recursive_subfolders=args.recursive,
                logger=logger,
                **parser_kwargs,
            )\
            .extract_for_training(
                audio_files_dir=args.audio_files_dir,
                export_dir=export_dir,
                audio_format=args.audio_output_ext,
                logger=logger,
                logfile_errors_path=os.path.join(export_dir, "error_log.txt"),
                logfile_success_path=os.path.join(export_dir, "success_log.txt"),
                label_settings_path = label_settings_path,
                resample=args.resample,
                overlap_s=args.chunk_overlap,
                date_format=args.date_format,
                late_start = args.late_start,
                early_stop = args.early_stop,
            )
        
            logger.print(f"... end of processing (elapsed {time.time()-ts:.1f}s)")
        except Exception as e:
            print()
            print("An error occured and the operation was not completed!")
            print(f"Check {logger.logfile_path} for more information.")
            logger.print_exception(e)  


    elif args.action == "train":
        subprocess.run(["python", "train.py"] + custom_args, cwd="BirdNET-Analyzer/")
    elif args.action=="validate":

        bnt_gt = Annotations(
            tables_dir=args.tables_dir_gt,
            table_format=args.table_format_gt,
            logger=Logger(),
            recursive_subfolders=args.recursive
        )

        bnt_tv = Annotations(
            tables_dir=args.tables_dir_tv,
            table_format=args.table_format_tv,
            logger=Logger(),
            recursive_subfolders=args.recursive
        )

        if args.confidence_thresholds is not None:
            thresholds = np.linspace(args.confidence_thresholds_start, args.confidence_thresholds_end, args.confidence_thresholds)
            for t in thresholds:
                print(t)
            exit()

        positive_labels = None
        if args.positive_labels is not None:
            positive_labels = args.positive_labels.split(",")
        stats_time, stats_count =  validate(
            ground_truth=bnt_gt,
            to_validate=bnt_tv,
            binary=args.binary,
            positive_labels=positive_labels,
            late_start = args.late_start,
            early_stop = args.early_stop
        )

        def save_stats(stats: tuple[pd.DataFrame, pd.DataFrame], suffix: str):
            fname = lambda f: os.path.join(args.output_dir, f"{f}_{suffix}.csv")
            df_matrix, df_metrics  = stats
            df_matrix.to_csv(fname("confusion_matrix"))
            df_metrics.to_csv(fname("validation_metrics"))
        
        save_stats(stats_time, "time")
        save_stats(stats_count, "count")

        


    

        


