from pydantic import BaseModel


class UsageSummary(BaseModel):
    days: int
    requests: int
    prompt_tokens: int
    completion_tokens: int
