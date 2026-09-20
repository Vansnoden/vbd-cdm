
from enum import Enum
from typing import Optional
from pydantic import BaseModel, ConfigDict
import pandas as pd


class ProcessResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    status: bool
    errors: Optional[dict] = None
    data: Optional[pd.DataFrame] = None

