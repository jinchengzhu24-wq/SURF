SYSTEM_PROMPT = (
    "You are a classic Sokoban level design director. Your job is "
    "to create a high-level blueprint for an algorithmic Sokoban "
    "level generator. Use classic design principles: compact rooms, "
    "corridors, choke points, goal-room pressure, route planning, "
    "reverse-design thinking, and deadlock avoidance. Do not copy "
    "or reproduce any existing online level. Return only valid JSON. "
    "Your archetype choice will select a hard local structure "
    "template, so choose intentionally. Do not generate map rows, "
    "coordinates, tile grids, markdown, or explanations."
)

BASE_USER_PROMPT = (
    "Create a fresh, classic-inspired hard blueprint "
    "for a 12x10 Sokoban level with exactly 2 boxes. The local "
    "algorithm will enforce solvability, wall templates, water "
    "placement, and tile rules. Pushes means box pushes, not "
    "player walking moves. Choose values inside these "
    "inclusive ranges: "
    "minSolutionSteps 18-30, maxSolutionSteps 32-50, "
    "minPushes 8-16, maxPushes 14-28, "
    "minWaterAreas 1-2, maxWaterAreas 1-2, "
    "minWallObstacleBlocks exactly 2, maxWallObstacleBlocks 2-3. "
    "minReversePulls 14-24, maxReversePulls 24-40. "
    "Each max value must be greater than or equal to its min value. "
    "Choose exactly one archetype from: goal_room, "
    "bottleneck_corridor, split_route, open_workshop. "
    "Choose exactly one targetLayout from: clustered, split_pair, "
    "edge_cluster. Choose exactly one obstacleStyle from: "
    "central_baffle, side_choke, goal_guard. Choose exactly one "
    "waterStyle from: corner_pool, side_pool, route_divider. "
    "Use a short style label and a short designNote. Return exactly "
    "these JSON keys: "
    "minSolutionSteps, maxSolutionSteps, minPushes, "
    "maxPushes, minWaterAreas, maxWaterAreas, minWallObstacleBlocks, "
    "maxWallObstacleBlocks, minReversePulls, maxReversePulls, "
    "style, archetype, targetLayout, obstacleStyle, waterStyle, "
    "designNote. "
)


def build_level_plan_messages(variation_seed, recent_blueprint_hint, creative_context=None):
    creative_context = creative_context or {}
    user_prompt = (
        BASE_USER_PROMPT
        + build_creative_idea_prompt(creative_context)
        + f"Variation seed: {variation_seed}. "
        + "Avoid these recent blueprint combinations if possible: "
        + f"{recent_blueprint_hint}."
    )

    return [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": user_prompt,
        },
    ]


EXPANSION_SYSTEM_PROMPT = (
    "You are a Sokoban design assistant. Expand a player's rough level idea "
    "into exactly three distinct, playable high-level design directions for a "
    "classic 12x10 Sokoban level with exactly 2 boxes. Do not generate map rows, "
    "tile grids, coordinates, markdown, or explanations outside JSON. Return "
    "only valid JSON."
)


def build_creative_idea_expansion_messages(creative_context):
    idea_text = normalize_prompt_text(creative_context.get("ideaText"))
    idea_id = normalize_prompt_text(creative_context.get("ideaId"))
    session_id = normalize_prompt_text(creative_context.get("sessionId"))
    scene_name = normalize_prompt_text(creative_context.get("sceneName"))
    context_parts = []

    if idea_id:
        context_parts.append(f"ideaId={idea_id}")

    if session_id:
        context_parts.append(f"sessionId={session_id}")

    if scene_name:
        context_parts.append(f"sceneName={scene_name}")

    context_text = ", ".join(context_parts) if context_parts else "none"
    user_prompt = (
        "Expand this player's Sokoban level idea into three options. "
        f"Original idea: \"{idea_text}\". "
        f"Context: {context_text}. "
        "Each option must primarily follow the original idea, but make it more "
        "specific for the supported generator. The supported generator can use "
        "high-level intent for difficulty, route shape, target layout, wall "
        "obstacles, water obstacles, choke points, and planning pressure. "
        "Do not promise unsupported mechanics. Keep language consistent with the "
        "player's input language. Return exactly this JSON shape: "
        "{\"options\":["
        "{\"id\":\"A\",\"title\":\"...\",\"description\":\"...\",\"promptText\":\"...\"},"
        "{\"id\":\"B\",\"title\":\"...\",\"description\":\"...\",\"promptText\":\"...\"},"
        "{\"id\":\"C\",\"title\":\"...\",\"description\":\"...\",\"promptText\":\"...\"}"
        "]}. "
        "title must be short. description should be one or two concise sentences "
        "for the player. promptText should be a compact generation instruction "
        "that can be combined with the original idea."
    )

    return [
        {
            "role": "system",
            "content": EXPANSION_SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": user_prompt,
        },
    ]


def build_creative_idea_prompt(creative_context):
    idea_text = normalize_prompt_text(creative_context.get("ideaText"))

    if not idea_text:
        return ""

    idea_id = normalize_prompt_text(creative_context.get("ideaId"))
    session_id = normalize_prompt_text(creative_context.get("sessionId"))
    scene_name = normalize_prompt_text(creative_context.get("sceneName"))
    context_parts = []

    if idea_id:
        context_parts.append(f"ideaId={idea_id}")

    if session_id:
        context_parts.append(f"sessionId={session_id}")

    if scene_name:
        context_parts.append(f"sceneName={scene_name}")

    context_text = ""

    if context_parts:
        context_text = " Context: " + ", ".join(context_parts) + "."

    return (
        "Player creative workshop idea: "
        f"\"{idea_text}\"."
        f"{context_text} "
        "Interpret this idea as high-level design intent for the blueprint. "
        "Respect the generator's supported schema and ranges even if the idea "
        "asks for details the generator cannot directly represent. "
    )


def normalize_prompt_text(value):
    if value is None:
        return ""

    text = str(value).strip()
    return " ".join(text.split())
