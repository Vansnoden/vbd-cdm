
from enum import Enum
from typing import Optional
from pydantic import BaseModel
import pandas as pd


class ProcessResult(BaseModel):
    status: bool
    errors: Optional[dict] = None
    data: Optional[pd.DataFrame] = None

