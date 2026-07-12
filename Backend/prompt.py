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
    "Choose corridorPlacement from: none, center, side. Choose corridorWidth "
    "as 0 when corridorPlacement is none, otherwise 1 or 2. Choose "
    "corridorOrientation from: horizontal, vertical, any. Choose corridorRole "
    "from: visual_only, player_route, required_box_route. Choose "
    "corridorPriority from: preferred, required. Use a short style label and "
    "a short designNote. Return exactly "
    "these JSON keys: "
    "minSolutionSteps, maxSolutionSteps, minPushes, "
    "maxPushes, minWaterAreas, maxWaterAreas, minWallObstacleBlocks, "
    "maxWallObstacleBlocks, minReversePulls, maxReversePulls, "
    "style, archetype, targetLayout, obstacleStyle, waterStyle, "
    "designNote, corridorPlacement, corridorWidth, corridorOrientation, "
    "corridorRole, corridorPriority. "
)


def build_level_plan_messages(variation_seed, recent_blueprint_hint, creative_context=None):
    creative_context = creative_context or {}
    user_prompt = (
        BASE_USER_PROMPT
        + build_prioritized_creative_context_prompt(creative_context)
        + f"Variation seed: {variation_seed}. "
        + "Recent-blueprint diversity is optional and must never override the "
        + "latest user adjustment or any required corridor field. Avoid these "
        + "recent blueprint combinations only if possible: "
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
    "classic 12x10 Sokoban level with exactly 2 boxes. Every option must be "
    "specific to the player's idea, not a generic template. Do not generate map "
    "rows, tile grids, coordinates, markdown, or explanations outside JSON. "
    "Return only valid JSON."
)


def build_creative_idea_expansion_messages(creative_context):
    idea_text = normalize_prompt_text(creative_context.get("ideaText"))
    idea_id = normalize_prompt_text(creative_context.get("ideaId"))
    session_id = normalize_prompt_text(creative_context.get("sessionId"))
    scene_name = normalize_prompt_text(creative_context.get("sceneName"))
    regeneration_attempt = normalize_prompt_int(creative_context.get("regenerationAttempt"))
    previous_options = normalize_previous_expansion_options(
        creative_context.get("previousOptions")
    )
    context_parts = []

    if idea_id:
        context_parts.append(f"ideaId={idea_id}")

    if session_id:
        context_parts.append(f"sessionId={session_id}")

    if scene_name:
        context_parts.append(f"sceneName={scene_name}")

    context_text = ", ".join(context_parts) if context_parts else "none"
    regeneration_text = build_expansion_regeneration_prompt(
        regeneration_attempt,
        previous_options,
    )
    user_prompt = (
        "Expand this player's Sokoban level idea into three options. "
        f"Original idea: \"{idea_text}\". "
        f"Context: {context_text}. "
        f"{regeneration_text}"
        "All three options must clearly preserve the original idea. Do not use "
        "a fixed trio such as narrow detour, split goals, and compact precision "
        "unless those concepts are directly suggested by the player's words. "
        "Make the options differ by concrete design angle: route structure, "
        "obstacle function, target placement, difficulty rhythm, or player "
        "experience. Avoid vague claims such as more challenging, more fun, or "
        "requires planning unless you immediately explain the exact board "
        "pressure that creates that feeling. The supported generator can use "
        "high-level intent for difficulty, route shape, target layout, wall "
        "obstacles, water obstacles, choke points, and planning pressure. "
        "Do not promise unsupported mechanics, story elements, enemies, keys, "
        "timers, moving parts, or visual-only themes. Keep language consistent "
        "with the player's input language. Return exactly this JSON shape: "
        "{\"options\":["
        "{\"id\":\"A\",\"title\":\"...\",\"description\":\"...\",\"promptText\":\"...\"},"
        "{\"id\":\"B\",\"title\":\"...\",\"description\":\"...\",\"promptText\":\"...\"},"
        "{\"id\":\"C\",\"title\":\"...\",\"description\":\"...\",\"promptText\":\"...\"}"
        "]}. "
        "title must be short and tailored to this idea. description must be one "
        "or two concrete player-facing sentences describing how the idea appears "
        "in the level. promptText is hidden from the player and should be a "
        "compact generation instruction that combines the original idea with "
        "the option's specific route, obstacle, target, and difficulty intent."
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


def build_expansion_regeneration_prompt(regeneration_attempt, previous_options):
    if regeneration_attempt <= 0:
        return ""

    prompt = (
        f"This is regeneration attempt {regeneration_attempt}. Generate a fresh "
        "replacement set of three options. Do not repeat or closely paraphrase "
        "any previous option title, route structure, obstacle function, target "
        "placement pattern, or difficulty rhythm. "
    )

    if previous_options:
        previous_text = "; ".join(
            format_previous_expansion_option(option, index)
            for index, option in enumerate(previous_options)
        )
        prompt += f"Previous options to avoid: {previous_text}. "

    return prompt


def format_previous_expansion_option(option, index):
    option_id = normalize_prompt_text(option.get("id")) or chr(ord("A") + index)
    title = normalize_prompt_text(option.get("title"))
    description = normalize_prompt_text(option.get("description"))
    prompt_text = normalize_prompt_text(option.get("promptText"))
    parts = []

    if title:
        parts.append(f"title={title}")

    if description:
        parts.append(f"description={description}")

    if prompt_text:
        parts.append(f"promptText={prompt_text}")

    if not parts:
        return f"{option_id}: empty"

    return f"{option_id}: " + ", ".join(parts)


def normalize_previous_expansion_options(value):
    if not isinstance(value, list):
        return []

    normalized = []

    for raw_option in value[:3]:
        if hasattr(raw_option, "model_dump"):
            option = raw_option.model_dump()
        elif isinstance(raw_option, dict):
            option = raw_option
        else:
            continue

        normalized_option = {
            "id": normalize_prompt_text(option.get("id"))[:12],
            "title": normalize_prompt_text(option.get("title"))[:80],
            "description": normalize_prompt_text(option.get("description"))[:180],
            "promptText": normalize_prompt_text(option.get("promptText"))[:220],
        }

        if any(normalized_option.values()):
            normalized.append(normalized_option)

    return normalized


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


def build_prioritized_creative_context_prompt(creative_context):
    creative_context = creative_context or {}
    idea_text = normalize_prompt_text(creative_context.get("ideaText"))
    original_idea = normalize_prompt_text(creative_context.get("originalIdeaText"))
    selected_direction = normalize_prompt_text(creative_context.get("selectedDirectionText"))
    refinement_feedback = normalize_prompt_text(creative_context.get("refinementFeedbackText"))
    adjustment_history = normalize_prompt_text(creative_context.get("adjustmentHistoryText"))
    latest_adjustment = normalize_prompt_text(creative_context.get("latestAdjustmentText"))
    parts = [
        "Follow this priority order: solvability and supported Sokoban rules; "
        "latest user adjustment; selected design direction; original user idea; "
        "earlier adjustments and refinement feedback; general difficulty and "
        "quality preferences; variation and recent-blueprint diversity. "
    ]

    if latest_adjustment:
        parts.append(
            f'Latest user adjustment (authoritative hard requirement): "{latest_adjustment}". '
            "When it conflicts with earlier intent, follow the latest adjustment "
            "while preserving as much earlier intent as possible. "
        )

    if selected_direction:
        parts.append(f'Selected design direction: "{selected_direction}". ')

    if original_idea:
        parts.append(f'Original user idea: "{original_idea}". ')

    if adjustment_history:
        parts.append(f'Earlier adjustment history (context only): "{adjustment_history}". ')

    if refinement_feedback:
        parts.append(f'Refinement feedback (context only): "{refinement_feedback}". ')

    if idea_text and not any((original_idea, selected_direction, latest_adjustment)):
        parts.append(f'Legacy combined user idea: "{idea_text}". ')

    parts.append(
        "Do not satisfy a structural request only through style or designNote; "
        "encode it in the corridor fields. A request for a narrow corridor in "
        "the map center must use archetype=bottleneck_corridor, "
        "obstacleStyle=central_baffle, corridorPlacement=center, "
        "corridorPriority=required, and corridorWidth=1 unless another width is "
        "explicitly requested. Use corridorRole=required_box_route only when the "
        "user says a box must pass through it; otherwise use player_route. "
    )
    return "".join(parts)


def normalize_prompt_text(value):
    if value is None:
        return ""

    text = str(value).strip()
    return " ".join(text.split())


def normalize_prompt_int(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
