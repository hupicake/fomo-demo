"""Strict planning and acceptance contracts owned by the Direct Pi runtime."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from fomo.schemas import SchemaModel

BoundedText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
    ),
]

_LOCAL_PATH = re.compile(r"^/[A-Za-z0-9/_-]*$")
_ALLOWED_ROLES = {
    "alert",
    "alertdialog",
    "button",
    "checkbox",
    "columnheader",
    "combobox",
    "dialog",
    "grid",
    "heading",
    "link",
    "list",
    "listitem",
    "main",
    "navigation",
    "option",
    "radio",
    "row",
    "searchbox",
    "spinbutton",
    "status",
    "switch",
    "tab",
    "table",
    "textbox",
}


class Locator(SchemaModel):
    by: Literal["role", "label", "text"]
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
        | None
    ) = None

    @model_validator(mode="after")
    def valid_locator(self) -> Locator:
        if self.by == "role":
            if self.value not in _ALLOWED_ROLES or not self.name:
                raise ValueError("role locators require an allowed role and an accessible name")
        elif self.name is not None:
            raise ValueError("only role locators may declare name")
        return self


class GotoAction(SchemaModel):
    kind: Literal["goto"]
    path: str

    @field_validator("path")
    @classmethod
    def local_path(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 200 or not _LOCAL_PATH.fullmatch(value):
            raise ValueError("goto path must be a bounded local path")
        return value


class ClickAction(SchemaModel):
    kind: Literal["click"]
    target: Locator


class FillAction(SchemaModel):
    kind: Literal["fill"]
    target: Locator
    value: Annotated[str, StringConstraints(max_length=1000)]


class SelectAction(SchemaModel):
    kind: Literal["select"]
    target: Locator
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ReloadAction(SchemaModel):
    kind: Literal["reload"]
    # Models often keep a uniform ``target`` slot across action variants.
    # Explicit null is unambiguous for reload; every non-null value remains
    # forbidden by the strict schema.
    target: None = None


AcceptanceAction = Annotated[
    GotoAction | ClickAction | FillAction | SelectAction | ReloadAction,
    Field(discriminator="kind"),
]


class VisibleAssertion(SchemaModel):
    kind: Literal["visible", "not_visible"]
    target: Locator


class ValueAssertion(SchemaModel):
    kind: Literal["value"]
    target: Locator
    expected: Annotated[str, StringConstraints(max_length=1000)]


class UrlAssertion(SchemaModel):
    kind: Literal["url"]
    path: str

    @field_validator("path")
    @classmethod
    def local_path(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 200 or not _LOCAL_PATH.fullmatch(value):
            raise ValueError("url assertion path must be a bounded local path")
        return value


AcceptanceAssertion = Annotated[
    VisibleAssertion | ValueAssertion | UrlAssertion,
    Field(discriminator="kind"),
]


class AcceptanceItem(SchemaModel):
    id: Identifier
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    priority: Literal["must", "should"] = "must"
    given: BoundedText
    when: BoundedText
    then: BoundedText


class AcceptanceTest(SchemaModel):
    id: Identifier
    acceptance_id: Identifier
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    actions: list[AcceptanceAction] = Field(min_length=1)
    assertions: list[AcceptanceAssertion] = Field(min_length=1)


class AcceptanceContract(SchemaModel):
    criteria: list[AcceptanceItem] = Field(min_length=1)
    tests: list[AcceptanceTest] = Field(min_length=1)

    @model_validator(mode="after")
    def exact_test_per_criterion(self) -> AcceptanceContract:
        criterion_ids = [item.id for item in self.criteria]
        test_ids = [item.id for item in self.tests]
        tested_ids = [item.acceptance_id for item in self.tests]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("acceptance criterion ids must be unique")
        if len(test_ids) != len(set(test_ids)):
            raise ValueError("acceptance test ids must be unique")
        if sorted(criterion_ids) != sorted(tested_ids):
            raise ValueError("each acceptance criterion must have exactly one test")
        return self
