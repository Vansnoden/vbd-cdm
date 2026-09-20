from abc import ABC, abstractmethod
import pandas as pd

from utilities.types import ProcessResult


class Extractor(ABC):
    @abstractmethod
    def extract_data(self, ext:str, source_file_path:str) -> ProcessResult:
        pass


class Transformer(ABC):
    @abstractmethod
    def transform(self, ext:str, source_file_path:str, mapping_file_path:str) -> ProcessResult:
        pass 


class Loader(ABC):
    @abstractmethod
    def load(self, source_data:pd.DataFrame, mapping_file_path:str) -> ProcessResult:
        pass
