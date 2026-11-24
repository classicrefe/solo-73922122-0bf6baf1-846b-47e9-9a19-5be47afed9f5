"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


# ---------------- 请求模型 ----------------

class OverlapIn(BaseModel):
    new_code: str = Field(..., min_length=1, description="新区编码")
    old_code: str = Field(..., min_length=1, description="旧区编码")
    building_total: int = Field(..., gt=0, description="该重叠范围楼栋总数（正整数）")


class NewDistrictIn(BaseModel):
    code: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    fee: int = Field(..., gt=0, description="调档费用，正整数")


class OldDistrictIn(BaseModel):
    code: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    completed: int = Field(..., ge=0, description="该旧区历年完工楼栋总数（底账）")


class PeriodSetupIn(BaseModel):
    code: str = Field(..., min_length=1, description="期次编码，如 2023")
    name: str = Field(..., min_length=1, description="期次名称")
    old_districts: list[OldDistrictIn] = Field(..., min_length=1)
    new_districts: list[NewDistrictIn] = Field(..., min_length=1, max_length=18)
    overlaps: list[OverlapIn] = Field(..., min_length=1)


class RetrievalIn(BaseModel):
    new_code: str = Field(..., min_length=1, description="所调档的新区")
    counts: dict[str, int] = Field(
        ...,
        description="该新区分属各旧区的完工数，键为旧区编码，值为非负整数",
    )


class DraftIn(BaseModel):
    counts: dict[str, int] = Field(
        ...,
        min_length=1,
        description="各新区草稿完工数，键为新区编码，值为非负整数；"
        "合计须等于各旧区底账完工数之和",
    )

    @field_validator("counts")
    @classmethod
    def _non_negative(cls, counts: dict[str, int]) -> dict[str, int]:
        bad = {k: v for k, v in counts.items() if v < 0}
        if bad:
            raise ValueError(f"草稿完工数必须为非负整数，非法项: {bad}")
        return counts


# ---------------- 响应模型 ----------------

class OverlapOut(BaseModel):
    new_code: str
    old_code: str
    building_total: int


class NewDistrictOut(BaseModel):
    code: str
    name: str
    fee: int


class OldDistrictOut(BaseModel):
    code: str
    name: str
    completed: int


class PeriodOut(BaseModel):
    code: str
    name: str
    old_districts: list[OldDistrictOut]
    new_districts: list[NewDistrictOut]
    overlaps: list[OverlapOut]


class RetrievalItemOut(BaseModel):
    new_code: str
    old_code: str
    completed_count: int


class RegisteredNewOut(BaseModel):
    new_code: str
    fee: int
    counts: dict[str, int]


class PlanOut(BaseModel):
    period_code: str
    status: str = Field(..., description="retrieval_required / completed / infeasible")
    plan: list[str] = Field(..., description="总费用最低的待调新区编码（有序）")
    total_fee: int
    registered: list[RegisteredNewOut]
    old_remaining: dict[str, int]
    message: str


class NewCompletedOut(BaseModel):
    new_code: str
    name: str
    completed: int


class AccountingOut(BaseModel):
    period_code: str
    status: str
    determined: bool
    new_completed: list[NewCompletedOut] | None = None
    total_completed: int | None = None
    plan: list[str] | None = None
    total_fee: int | None = None
    old_remaining: dict[str, int]
    message: str


class MessageOut(BaseModel):
    detail: str


# ---------------- 完工草稿 ----------------

class DraftItemOut(BaseModel):
    new_code: str
    name: str
    completed: int


class DraftOut(BaseModel):
    period_code: str
    submitted_at: str | None = Field(None, description="最近一次提交时间；无草稿时为 null")
    items: list[DraftItemOut]
    total: int = Field(..., description="草稿合计，须等于底账合计")
    ledger_total: int


class DraftSubmitOut(BaseModel):
    period_code: str
    replaced: bool = Field(..., description="true=替换了原稿；false=首次提交")
    submitted_at: str
    items: list[DraftItemOut]
    total: int
    ledger_total: int
    message: str


# ---------------- 改数单 ----------------

class NewAdjustmentOut(BaseModel):
    new_code: str
    name: str
    draft_completed: int = Field(..., description="草稿完工数")
    final_completed: int = Field(..., description="改数单确定的完工数")
    delta: int = Field(..., description="增减数 = 改数单 - 草稿，负数表示调减")


class OverlapCompletedOut(BaseModel):
    new_code: str
    old_code: str
    building_total: int = Field(..., description="该重叠范围楼栋总数")
    completed_count: int = Field(..., description="该范围完工数：已查照录，未查为求解结果")
    source: str = Field(..., description="retrieved=档案照录 / solved=未查范围求解")


class ChangeOrderOut(BaseModel):
    period_code: str
    status: str = Field(..., description="ok / infeasible")
    has_draft: bool
    total_reduction: int | None = Field(
        None, description="最小调减总数（各新区比草稿调减栋数之和）"
    )
    total_increase: int | None = Field(None, description="调增总数，恒等于调减总数")
    adjustments: list[NewAdjustmentOut] | None = None
    overlap_completed: list[OverlapCompletedOut] | None = None
    ledger_total: int
    final_total: int | None = Field(None, description="改数单各新区完工合计，对得上底账")
    message: str
