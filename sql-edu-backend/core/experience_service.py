"""
Student Experience and Leveling Service.

This module provides utility functions to manage student progression:
- Calculate experience points (XP) required for the next level.
- Convert cumulative experience points into the corresponding level and progress.
- Compute the XP reward when a student successfully solves a problem.
"""


def xp_for_next_level(level: int) -> int:
    """
    Calculates the experience points required to advance from the given level to the next.

    Args:
        level (int): The current level of the user.

    Returns:
        int: The total experience points required inside the current level to rank up.
    """
    if level < 1:
        return 100
    # Leveling curve: base of 100 XP, increasing by 50 XP for each subsequent level
    return 100 + 50 * (level - 1)


def get_level_from_total(total_experience: int) -> tuple[int, int, int]:
    """
    Calculates the current level, progress, and next level threshold based on total accumulated XP.

    Args:
        total_experience (int): The absolute total experience accumulated by the student.

    Returns:
        tuple[int, int, int]: A tuple containing:
            - level: Current active level (starting at 1).
            - experience_in_level: XP acquired within the current level (current bar value).
            - xp_to_next_level: Total XP required to reach the next level (bar max value).
    """
    if total_experience <= 0:
        return 1, 0, xp_for_next_level(1)
    level = 1
    consumed = 0
    # Iterate and subtract required experience for each level until remaining XP is less than next level requirement
    while True:
        need = xp_for_next_level(level)
        if consumed + need > total_experience:
            exp_in_level = total_experience - consumed
            return level, exp_in_level, need
        consumed += need
        level += 1


def compute_xp_gain(
    question_difficulty: int,
    chat_count: int,
    wrong_attempts_before_correct: int,
    challenge_mode: bool,
) -> int:
    """
    Computes the experience points gained by a student for their first correct answer to a question.

    Factors considered in calculations:
    - Base completion XP (25).
    - Bonus based on question difficulty level (ranges 1 to 10).
    - Bonus for interactions with the AI tutor (scaled by chat count).
    - Bonus for resilience (wrong attempts count, representing persistence).
    - Challenge mode multiplier (+50% bonus).

    Args:
        question_difficulty (int): The difficulty level of the question (1 to 10).
        chat_count (int): The number of chat messages exchanged with the AI during this question.
        wrong_attempts_before_correct (int): Count of incorrect submissions made before the correct one.
        challenge_mode (bool): Flag indicating if the submission was made in challenge mode.

    Returns:
        int: Total XP gained, capped between 1 and 80.
    """
    # Base experience awarded just for completing the task successfully
    base = 25
    # Difficulty bonus: scaled linearly (approx +3 per difficulty tier), capped at 27
    difficulty_bonus = min(27, max(0, (question_difficulty - 1) * 3))
    # Engagement bonus: +1 XP per 2 chat messages, capped at 12 XP (representing approx 24 messages)
    chat_bonus = min(12, chat_count // 2)
    # Resilience bonus: +1 XP per wrong attempt, capped at 8 XP to avoid rewarding deliberate failures
    attempt_bonus = min(8, wrong_attempts_before_correct)
    total = base + difficulty_bonus + chat_bonus + attempt_bonus
    # Provide a 50% XP boost if the question was solved under a timer in challenge mode
    if challenge_mode:
        total = int(total * 1.5)
    # Clamp final reward to keep progression rates balanced and predictable
    return max(1, min(80, total))  # Single question XP is capped between 1 and 80


__all__ = ["xp_for_next_level", "get_level_from_total", "compute_xp_gain"]
