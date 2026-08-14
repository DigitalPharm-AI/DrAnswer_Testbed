"""Source-controlled feature switches for testbed scenario variants."""

# False hides NutritionRecommendationAgent delegation while keeping meal records,
# nutrition summaries, and preference management available.
NUTRITION_RECOMMENDATION_ENABLED = False

# False disables medication side-effect assessment, history, questionnaires,
# and record creation while keeping adherence and dose-taking features active.
MEDICATION_SIDE_EFFECT_ENABLED = False
