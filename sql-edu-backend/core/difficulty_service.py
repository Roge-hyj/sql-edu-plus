"""
Question Difficulty and Suggested Time Calculator Service.

This module dynamically computes the display difficulty of questions by combining:
- Teacher's initial configuration difficulty.
- Objective performance statistics (total submissions, correct submissions, chat count).
- Subjective feedback from students.
It also calculates suggested challenge duration based on the display difficulty.
"""


def compute_display_difficulty(
    teacher_difficulty: int,
    total_submissions: int,
    correct_submissions: int,
    total_chat_messages: int,
    feedback_count: int,
    avg_student_rating: float | None,
) -> float:
    """
    Computes a dynamic, integrated difficulty rating (1.0 to 10.0) for a question.

    The computation uses a weighted system where the teacher's base difficulty is 
    predominant when data is scarce, while historical student performance (submission rates,
    AI conversation volume) and student feedback gain weight as usage increases.

    Args:
        teacher_difficulty (int): The initial difficulty assigned by the teacher (1 to 10).
        total_submissions (int): The total number of student answers submitted.
        correct_submissions (int): The count of correct student answers.
        total_chat_messages (int): The total count of AI tutor messages exchanged on this question.
        feedback_count (int): The number of student ratings submitted.
        avg_student_rating (float | None): The average difficulty rating given by students (1 to 10).

    Returns:
        float: Calculated display difficulty rounded to one decimal place, within [1.0, 10.0].
    """
    # Bound base teacher difficulty between 1 and 10
    base = float(max(1, min(10, teacher_difficulty)))

    # 1. Objective component calculation: ratio of total/correct submissions + AI dialogue intensity
    submissions = total_submissions or 0
    correct = correct_submissions or 0
    chats = total_chat_messages or 0
    raw_objective = 0.0
    
    if correct > 0:
        # If there are correct answers, base the objective difficulty on attempts per success 
        # and avg chat logs generated per success (scaled to prevent outliers from inflating it too much)
        raw_objective = (submissions / correct) * 0.5 + min(chats / max(correct, 1) * 0.2, 4.0)
    else:
        # If no correct answers are logged yet, approximate difficulty based on submission volume
        raw_objective = min(submissions * 0.1 + chats * 0.01, 9.0)
        
    objective_norm = max(1.0, min(10.0, 1.0 + raw_objective))

    # 2. Subjective component: student feedback is already rated from 1 to 10
    if avg_student_rating is not None and feedback_count > 0:
        subjective_norm = max(1.0, min(10.0, avg_student_rating))
    else:
        subjective_norm = base

    # Determine dynamic weight based on data volume
    # As the sample size grows, we trust student telemetry and ratings more than the static teacher configuration
    n = total_submissions + feedback_count
    if n < 5:
        w = 0.1
    elif n < 15:
        w = 0.25
    elif n < 40:
        w = 0.4
    else:
        w = 0.55

    # Merge teacher's baseline with average of objective and subjective metrics
    combined = (1.0 - w) * base + w * (0.5 * objective_norm + 0.5 * subjective_norm)
    return round(max(1.0, min(10.0, combined)), 1)


def suggested_time_seconds(display_difficulty: float, _teacher_time_limit: int | None = None) -> int:
    """
    Suggests a challenge timer limit in seconds based on the display difficulty.

    Args:
        display_difficulty (float): The calculated display difficulty rating (1.0 to 10.0).
        _teacher_time_limit (int | None, optional): Deprecated parameter for custom time limit override.

    Returns:
        int: Recommended time limit in seconds, ranging from 180s (3m) to 600s (10m).
    """
    d = max(1.0, min(10.0, display_difficulty))
    # Map [1.0, 10.0] difficulty linearly to [180s, 600s] duration (3 to 10 minutes)
    return int(120 + 48 * d)
