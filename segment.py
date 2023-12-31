from units import TimeUnit
from variables import BIRDNET_AUDIO_DURATION
import  intervaltree as it


class Segment(it.Interval):
    """
    Extends the `intervaltree.Interval` class. Uses `bytearray` to represent the 
    label string in the data, in order to make it mutable.
    """
    def __new__(cls, tstart_s: float, tend_s: float, label: str | bytearray = "", line_number: int = 0, *args, **kwargs):
        if isinstance(label, str):
            label = label.encode()
        if label is None:
            label = b""
        instance = super(Segment, cls).__new__(cls, tstart_s, tend_s, bytearray(label))
        instance.line_number = line_number
        return instance
    
    def __deepcopy__(self, memo):
        cls = self.__class__
        result = cls(*self._get_fields())
        memo[id(self)] = result
        return result

    def __copy__(self):
        return self.__class__(*self._get_fields())
    
    def __reduce__(self):
        return self.__class__, self._get_fields()

    @property
    def tstart(self):
        return TimeUnit(self.begin)
    
    @property
    def tend(self):
        return TimeUnit(self.end)

    @property
    def label(self):
        return self.data.decode()
    
    @label.setter
    def label(self, l: str):
        self.data[:] = l.encode()
        
    @property
    def dur(self) -> TimeUnit:
        return self.tend - self.tstart
    
    def centered_pad(self, pad: TimeUnit):
        return Segment(self.tstart - pad, self.tend + pad, self.label)
    
    def safe_centered_pad(self, pad: TimeUnit):
        if self.tstart-pad < 0:
            return Segment(0, self.tend + pad + (pad - self.tstart), self.label)
        return self.centered_pad(pad)

    def centered_pad_to(self, duration: TimeUnit):
        if self.dur > duration:
            return self
        return self.safe_centered_pad((duration - self.dur)/2)
    
    def birdnet_pad(self):
        return self.centered_pad_to(BIRDNET_AUDIO_DURATION)

    def overlapping_time(self, other: 'Segment'):
        # Copied from Pshemek
        ov_b = max(self.tstart, other.tstart)
        ov_e = min(self.tend, other.tend)
        return max(0.0, ov_e - ov_b)

    def overlapping_perc(self, other: 'Segment'):
        return self.overlapping_time(other)/self.dur
    

    def __repr__(self):
        seg_name = f" \"{self.label}\"" if self.label is not None else ""
        return f"Segment{seg_name}: [{self.tstart.time_str(True)}, {self.tend.time_str(True)}]"
    
    @staticmethod
    def from_interval(interval: it.Interval):
        """
        Create a segment from an a treeinterval Interval.
        """
        return Segment(interval.begin, interval.end, interval.data)

    @staticmethod
    def get_intervaltree(segments: list['Segment']):
        """
        Returns the IntervalTree datastructure from a list of segments.
        """
        return it.IntervalTree(segments)

    

def durSegment(tstart_s: float, dur_s: float, label, *args, **kwargs):
    return Segment(tstart_s, tstart_s+dur_s, label, *args, **kwargs)
    

class ConfidenceSegment(Segment):
    def __init__(self, tstart_s: float, tend_s: float, label: str | bytearray = "", confidence = 1, *args, **kwargs):
        self.confidence = float(confidence)

    def __repr__(self):
        return f"{super().__repr__()} Confidence: {str(self.confidence)}"
    
    def _get_fields(self):
        return self.tstart, self.tend, self.label, self.confidence





def confidenceDurSegment(tstart_s: float, dur_s: float, label: str | bytearray = "", confidence = 1, *args, **kwargs):
    return ConfidenceSegment(tstart_s, tstart_s+dur_s, label, confidence, *args, **kwargs)




class ConfidenceFreqSegment(ConfidenceSegment):
    def __init__(self, tstart_s: float, dur: float, label: str | bytearray = "", fstart = 0, fend = 15000, confidence = 1, *args, **kwargs):
        self.confidence = float(confidence)
        self.fstart = fstart
        self.fend = fend

    def __repr__(self):
        return f"{super().__repr__()} Frequencies: [{self.fstart:.4f}, {self.fend:.4f}] Confidence: {str(self.confidence)}"

    def _get_fields(self):
            return self.tstart, self.tend, self.label, self.fstart, self.fend, self.confidence



def confidenceDurFreqSegment(tstart_s: float, dur_s: float, label: str | bytearray = "", fstart = 0, fend = 15000, confidence = 1, *args, **kwargs):
    return ConfidenceFreqSegment(tstart_s, tstart_s+dur_s, label, label, fstart, fend, confidence, *args, **kwargs)








