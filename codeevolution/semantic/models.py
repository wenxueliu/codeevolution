from dataclasses import dataclass, field


@dataclass
class BusinessDescription:
    function_name: str
    summary_en: str
    summary_zh: str
    business_domain: str
    role: str
    key_responsibilities: list[str] = field(default_factory=list)


@dataclass
class BusinessRule:
    function_name: str
    rule_type: str
    description_en: str
    description_zh: str
    condition: str
    failure_mode: str


@dataclass
class ErrorScenario:
    function_name: str
    error_type: str
    trigger_condition: str
    handling: str
    user_facing: bool


@dataclass
class StateMachineDef:
    entity: str
    states: list[str]
    transitions: list[dict]
    initial_state: str
    terminal_states: list[str]
