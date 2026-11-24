"""Select a domain capability set; native DSH supplies the execution loop."""


def create_tools(kind, deps, record, emit):
    from .plan import PlanTools
    from .qa import QuestionAnswerTools
    from .quiz import QuizTools
    from .tutor import TutorTools

    factories = {
        "qa": QuestionAnswerTools,
        "quiz": QuizTools,
        "plan": PlanTools,
        "tutor": TutorTools,
    }
    return factories[kind](deps, record, emit)
