"""Select a domain capability set; native DSH supplies the execution loop."""


def create_tools(kind, deps, record, emit, collaboration=None):
    from .guided_plan import GuidedPlanTools
    from .plan import PlanTools
    from .qa import QuestionAnswerTools
    from .quiz import QuizTools
    from .team_tools import PlanCoordinatorTools, PlannerTools, PlanReviewerTools, ResearchTools
    from .tutor import TutorTools

    if kind == "plan" and collaboration is not None:
        return PlanCoordinatorTools(deps, record, emit, collaboration)
    if kind == "plan" and record.state.get("_architecture") == "guided":
        return GuidedPlanTools(deps, record, emit)

    factories = {
        "qa": QuestionAnswerTools,
        "quiz": QuizTools,
        "plan": PlanTools,
        "tutor": TutorTools,
        "researcher": ResearchTools,
        "planner": PlannerTools,
        "reviewer": PlanReviewerTools,
    }
    return factories[kind](deps, record, emit)
