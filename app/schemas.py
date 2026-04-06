from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator


class PracticeCreate(BaseModel):
    date: date
    attempted_questions: int = Field(gt=0)
    correct_questions: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_correct_not_more_than_attempted(self):
        if self.correct_questions > self.attempted_questions:
            raise ValueError("Correct questions cannot exceed attempted questions.")
        if self.date > date.today():
            raise ValueError("Future date entries are not allowed.")
        return self


class PracticeOut(BaseModel):
    id: int
    date: date
    attempted_questions: int
    correct_questions: int
    accuracy: float
    created_at: datetime

    class Config:
        from_attributes = True


class ExamMarkCreate(BaseModel):
    date: date
    marks_obtained: float = Field(ge=0)
    total_marks: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_obtained_not_more_than_total(self):
        if self.marks_obtained > self.total_marks:
            raise ValueError("Marks obtained cannot exceed total marks.")
        if self.date > date.today():
            raise ValueError("Future date entries are not allowed.")
        return self


class ExamMarkOut(BaseModel):
    id: int
    date: date
    marks_obtained: float
    total_marks: float
    percentage: float
    created_at: datetime

    class Config:
        from_attributes = True


class IncorrectRevisionCreate(BaseModel):
    date: date
    revised_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_date_not_future(self):
        if self.date > date.today():
            raise ValueError("Future date entries are not allowed.")
        return self


class IncorrectRevisionOut(BaseModel):
    id: int
    date: date
    revised_count: int
    created_at: datetime

    class Config:
        from_attributes = True


class WeeklyGoalCreate(BaseModel):
    week_start_date: date
    questions_target: int = Field(gt=0)
    mock_score_target: float = Field(gt=0, le=100)

    @model_validator(mode="after")
    def validate_week_start_date_not_future(self):
        if self.week_start_date > date.today():
            raise ValueError("Week start date cannot be in the future.")
        return self


class WeeklyGoalOut(BaseModel):
    id: int
    week_start_date: date
    questions_target: int
    mock_score_target: float
    created_at: datetime

    class Config:
        from_attributes = True


class MarrowIngestCreate(BaseModel):
    date: date
    attempted_questions: int = Field(gt=0)
    correct_questions: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_data(self):
        if self.correct_questions > self.attempted_questions:
            raise ValueError("Correct questions cannot exceed attempted questions.")
        if self.date > date.today():
            raise ValueError("Future date entries are not allowed.")
        return self


class SignupCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=6, max_length=128)


class LoginCreate(BaseModel):
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=6, max_length=128)


class GoogleAuthCreate(BaseModel):
    credential: str = Field(min_length=10)


class AuthResponse(BaseModel):
    access_token: str
    token_type: str
    user: dict

