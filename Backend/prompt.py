import json


ENGLISH_ONLY_OUTPUT_RULE = (
    "Understand user input written in any language, but translate its meaning "
    "internally and write every JSON string value in English using ASCII "
    "characters only. Never echo, quote, or preserve non-English user text in "
    "the response. This rule applies to visible text, hidden contracts, style, "
    "notes, reasons, identifiers, and every other string value. "
)


SYSTEM_PROMPT = (
    ENGLISH_ONLY_OUTPUT_RULE
    + "You are a classic Sokoban level design director. Your job is "
    "to create a high-level blueprint for an algorithmic Sokoban "
    "level generator. Use classic design principles: compact rooms, "
    "corridors, choke points, goal-room pressure, route planning, "
    "reverse-design thinking, and deadlock avoidance. Do not copy "
    "or reproduce any existing online level. Return only valid JSON. "
    "Your archetype choice will select a hard local structure "
    "template, so choose intentionally. Do not generate map rows, "
    "coordinates, tile grids, markdown, or explanations."
)


HUMAN_ADJUSTMENT_CLARITY_SYSTEM_PROMPT = (
    ENGLISH_ONLY_OUTPUT_RULE
    + "You are a clarity gate for a HUMAN-led Sokoban revision workflow. "
    "The user, not the AI, must diagnose the problem and choose the revision. "
    "Score only how actionable the submitted revision instruction is. Do not "
    "diagnose the level, propose a solution, rewrite the instruction, or add a "
    "design idea. Use this exact rubric, with each dimension scored 0, 1, or 2: "
    "problemScore: 0=no problem or goal stated, 1=general feeling or goal, "
    "2=specific problem in the current level; targetScore: 0=no change target, "
    "1=a broad dimension such as difficulty, 2=a supported target such as pushes, "
    "goals, corridor, walls, water, route structure, or box order; directionScore: "
    "0=no desired direction, 1=general more/less/easier/harder direction, "
    "2=a concrete transformation such as add, remove, separate, cluster, narrow, "
    "widen, preserve, or prohibit; detailScore: 0=no operational detail, "
    "1=one placement, amount, relation, or preservation detail, 2=multiple such "
    "details. totalScore is the sum. isClear is true only when totalScore is at "
    "least 4, targetScore is at least 1, and directionScore is at least 1. "
    "This is the routing gate: isClear=false sends the player to "
    "Clarification(Human), while isClear=true allows level generation to continue. "
    "A feeling alone, such as 'too easy', is not clear. 'Make it harder' is also "
    "not clear because it does not choose a level feature to change. Return only "
    "JSON with exactly these keys: problemScore, targetScore, directionScore, "
    "detailScore, totalScore, isClear, reason. reason must be one short sentence "
    "describing what information is missing without suggesting a revision."
)


GENERATOR_CAPABILITY_CONTRACT = (
    "Generator capability contract: the final level is always a static classic "
    "12x10 Sokoban board with exactly 1 player, 2 boxes, and 2 targets. It can "
    "use supported walls, 1-2 rectangular water areas, and at most one straight "
    "horizontal or vertical corridor of width 1 or 2 placed at the center or "
    "side. It cannot guarantee exact coordinates, exact box starting alignment, "
    "literal T/L/S-shaped walls, multiple required corridors, diagonal corridors, "
    "or a one-tile-wide water river. It does not support ice, teleporters, keys, "
    "doors, switches, enemies, timers, moving parts, dynamic walls, or different "
    "box types. Walls and water are permanent impassable tiles for both the player "
    "and boxes. A box can only occupy ordinary floor or a target: it cannot act as "
    "a bridge or stepping stone, activate a tile, remotely open or close a route, "
    "change a wall or water tile, or cross water. It may only block or vacate its "
    "own floor position through normal pushing. Before expanding or encoding an idea, "
    "adapt unsupported requests "
    "to the closest supported classic Sokoban pressure: convert any box/target "
    "count to exactly 2 while preserving ordering or interaction intent; compress "
    "other map sizes into 12x10; convert multiple, curved, diagonal, or wider "
    "corridors into one width-1-or-2 axis-aligned main corridor; convert dynamic "
    "mechanics into static wall, water, choke-point, detour, standing-position, "
    "or box-order pressure; and convert exact geometry into a high-level layout "
    "preference. A user-authored request for no water or no internal walls is "
    "supported through explicit zero-value constraints. Never promise an unsupported "
    "feature in player-visible text, style, designNote, or promptText. Preserve the "
    "user's underlying planning intention after adaptation. "
)

BASE_USER_PROMPT = (
    GENERATOR_CAPABILITY_CONTRACT
    + "Create a fresh, classic-inspired hard blueprint "
    "for a 12x10 Sokoban level with exactly 2 boxes. The local "
    "algorithm will enforce solvability, wall templates, water "
    "placement, and tile rules. Pushes means box pushes, not "
    "player walking moves. Choose values inside these "
    "inclusive ranges: "
    "minSolutionSteps 18-30, maxSolutionSteps 32-50, "
    "minPushes 8-16, maxPushes 14-28, "
    "minReversePulls 14-24, maxReversePulls 24-40. "
    "Each max value must be greater than or equal to its min value. "
    "Choose exactly one archetype from: goal_room, "
    "bottleneck_corridor, split_route, open_workshop. "
    "Interpret archetypes as follows: goal_room concentrates pressure near "
    "goals; bottleneck_corridor creates a narrow route; split_route separates "
    "box routes; open_workshop preserves wider maneuvering space. "
    "Choose exactly one targetLayout from: clustered, split_pair, "
    "edge_cluster. targetLayout controls actual target placement: clustered "
    "keeps goals close, split_pair separates them, and edge_cluster places "
    "them near an edge. Choose exactly one obstacleStyle from: "
    "central_baffle, side_choke, goal_guard. Choose exactly one "
    "waterStyle from: corner_pool, side_pool, route_divider. "
    "obstacleStyle and waterStyle are placement preferences, not guaranteed "
    "route mechanics. style and designNote are descriptive only and do not "
    "affect generation. "
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


ZERO_WATER_PHRASES = (
    "no water",
    "no-water",
    "without water",
    "without any water",
    "water-free",
    "water free",
    "zero water",
    "remove water",
    "remove the water",
    "don't use water",
    "do not use water",
    "不要水",
    "不要有水",
    "不使用水",
    "没有水",
    "无水",
    "去掉水",
    "不要水",
    "不要有水",
    "不要任何水",
    "不需要水",
    "不用水",
    "不使用水",
    "不放水",
    "没有水",
    "无水",
    "零水",
    "去掉水",
    "移除水",
    "删除水",
)

ZERO_INTERNAL_WALL_PHRASES = (
    "no internal wall",
    "no-internal-wall",
    "without internal wall",
    "without any internal wall",
    "zero internal wall",
    "no interior wall",
    "without interior wall",
    "no wall obstacle",
    "without wall obstacle",
    "without water or internal wall",
    "without any water or any internal wall",
    "no water or internal wall",
    "no water and no internal wall",
    "remove internal wall",
    "remove interior wall",
    "remove wall obstacle",
    "don't use internal wall",
    "do not use internal wall",
    "不要内部墙",
    "不使用内部墙",
    "没有内部墙",
    "无内部墙",
    "不要墙障碍",
    "去掉内部墙",
    "不要内部墙",
    "不要有内部墙",
    "不要任何内部墙",
    "不需要内部墙",
    "不使用内部墙",
    "不放内部墙",
    "没有内部墙",
    "无内部墙",
    "零内部墙",
    "不要墙障碍",
    "无墙障碍",
    "零墙障碍",
    "无水域和内部墙",
    "无水和内部墙",
    "去掉内部墙",
    "移除内部墙",
    "删除内部墙",
)

WATER_FEATURE_TERMS = (
    "water",
    "river",
    "pool",
    "lake",
    "pond",
    "水",
    "河",
    "湖",
    "池",
    "水",
    "河",
    "池",
    "湖",
)

INTERNAL_WALL_FEATURE_TERMS = (
    "wall",
    "internal wall",
    "interior wall",
    "wall obstacle",
    "墙",
    "墙体",
    "内部墙",
    "墙障碍",
    "墙",
    "墙体",
    "内部墙",
    "墙障碍",
)


def resolve_zero_feature_constraints(creative_context=None):
    snippets = build_user_constraint_snippets(creative_context or {})
    return {
        "noWater": resolve_feature_zero_request(
            snippets,
            ZERO_WATER_PHRASES,
            WATER_FEATURE_TERMS,
        ),
        "noInternalWalls": resolve_feature_zero_request(
            snippets,
            ZERO_INTERNAL_WALL_PHRASES,
            INTERNAL_WALL_FEATURE_TERMS,
        ),
    }


def build_user_constraint_snippets(creative_context):
    snippets = []
    latest_adjustment = normalize_prompt_text(
        creative_context.get("latestAdjustmentText")
    )

    if latest_adjustment:
        snippets.append(latest_adjustment)

    style_description = normalize_prompt_text(
        creative_context.get("styleDescription")
    )

    if style_description:
        snippets.append(style_description)

    raw_history = creative_context.get("adjustmentHistoryText")

    if raw_history is not None:
        history_entries = [
            normalize_prompt_text(entry)
            for entry in str(raw_history).splitlines()
            if normalize_prompt_text(entry)
        ]
        snippets.extend(reversed(history_entries))

    original_idea = normalize_prompt_text(creative_context.get("originalIdeaText"))

    if original_idea:
        snippets.append(original_idea)
    else:
        legacy_idea = normalize_prompt_text(creative_context.get("ideaText"))

        if legacy_idea:
            snippets.append(legacy_idea)

    return snippets


def resolve_feature_zero_request(snippets, zero_phrases, feature_terms):
    for snippet in snippets:
        normalized = normalize_prompt_text(snippet).casefold()

        if any(phrase in normalized for phrase in zero_phrases):
            return True

        if any(term in normalized for term in feature_terms):
            return False

    return False


def build_feature_constraint_prompt(feature_constraints):
    feature_constraints = feature_constraints or {}
    no_water = bool(feature_constraints.get("noWater"))
    no_internal_walls = bool(feature_constraints.get("noInternalWalls"))
    parts = []

    if no_water:
        parts.append(
            "The user explicitly requires no water. Set minWaterAreas=0 and "
            "maxWaterAreas=0 exactly. Do not describe or imply any water feature. "
        )
    else:
        parts.append(
            "The user did not explicitly require no water. Choose minWaterAreas "
            "and maxWaterAreas inside 1-2. "
        )

    if no_internal_walls:
        parts.append(
            "The user explicitly requires no internal walls. Set "
            "minWallObstacleBlocks=0 and maxWallObstacleBlocks=0 exactly. This "
            "requirement also forbids corridor divider walls, so set "
            "corridorPlacement=none, corridorWidth=0, corridorOrientation=any, "
            "corridorRole=visual_only, and corridorPriority=preferred. Only the "
            "closed outer shell may use walls. Do not describe or imply internal "
            "walls, choke walls, or corridors. "
        )
    else:
        parts.append(
            "The user did not explicitly require no internal walls. Set "
            "minWallObstacleBlocks=2 exactly and choose maxWallObstacleBlocks "
            "inside 2-3. "
        )

    return "".join(parts)


def build_level_plan_messages(
    variation_seed,
    recent_blueprint_hint,
    creative_context=None,
    feature_constraints=None,
):
    creative_context = creative_context or {}
    feature_constraints = feature_constraints or resolve_zero_feature_constraints(
        creative_context
    )
    user_prompt = (
        BASE_USER_PROMPT
        + build_feature_constraint_prompt(feature_constraints)
        + build_generation_preferences_prompt(
            creative_context.get("generationPreferences")
        )
        + build_prioritized_creative_context_prompt(
            creative_context,
            feature_constraints,
        )
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


def build_generation_preferences_prompt(generation_preferences=None):
    preferences = generation_preferences or {}

    if not preferences:
        return ""

    ordered_fields = (
        "minSolutionSteps",
        "maxSolutionSteps",
        "minPushes",
        "maxPushes",
        "minWaterAreas",
        "maxWaterAreas",
        "minWallObstacleBlocks",
        "maxWallObstacleBlocks",
        "minReversePulls",
        "maxReversePulls",
        "archetype",
        "targetLayout",
        "obstacleStyle",
        "waterStyle",
        "corridorPlacement",
        "corridorWidth",
        "corridorOrientation",
        "corridorRole",
        "corridorPriority",
    )
    assignments = [
        f"{field}={preferences[field]}"
        for field in ordered_fields
        if field in preferences
    ]

    if not assignments:
        return ""

    return (
        "The player explicitly selected these Description-to-Level generation "
        "parameters: "
        + "; ".join(assignments)
        + ". These values are authoritative hard constraints and override "
        "general defaults, recent-blueprint diversity, and any conflicting "
        "wording in the style description. Copy every listed value into the "
        "same-named JSON field exactly. Unlisted fields remain available for "
        "design interpretation. "
    )


EXPANSION_SYSTEM_PROMPT = (
    ENGLISH_ONLY_OUTPUT_RULE
    + "You are a Sokoban design assistant. Expand a player's rough level idea "
    "into exactly three distinct, playable high-level design directions for a "
    "classic 12x10 Sokoban level with exactly 2 boxes. Every option must be "
    "specific to the player's idea, not a generic template. The expansion shown "
    "to the player is a promise about the level that can actually be generated, "
    "so describe only observable results supported by the current generator. "
    "First identify the player's core desired experience internally. If the "
    "literal idea uses unsupported mechanics, translate them into supported "
    "classic Sokoban spatial pressure such as route choice, bottlenecks, wall "
    "obstacles, rectangular water areas, target placement, standing-position "
    "pressure, or box order. All three options must preserve that same core "
    "experience while offering three different supported spatial implementations. "
    "Do not output your analysis. Do not generate map rows, tile grids, coordinates, "
    "markdown, or explanations outside JSON. Return only valid JSON."
)


HA_REVISION_PLAN_SYSTEM_PROMPT = (
    ENGLISH_ONLY_OUTPUT_RULE
    + "You are the AI planning partner in a Human-AI Sokoban revision workflow. "
    "The human has already identified what they want to improve. Use the supplied "
    "previous LevelDesignPlan and corridor verification as the factual baseline. "
    "Return exactly three distinct, implementable revision plans. Every plan must "
    "honor the human's explicit adjustment and preserve every unlisted blueprint "
    "field. You may change only the smallest coherent set of causally necessary "
    "related fields. Do not diagnose an unrelated problem, add unsupported mechanics, "
    "generate a tile map, or expose the hidden contract in the visible description. "
    "Return only valid JSON."
)


HA_REVISION_PLAN_EDIT_SYSTEM_PROMPT = (
    ENGLISH_ONLY_OUTPUT_RULE
    + "You revise one selected option in a Human-AI Sokoban workflow. The player "
    "edited the visible option description, and that edit is authoritative. Convert "
    "it into one supported, minimal LevelDesignPlan change contract using the "
    "previous plan as the factual baseline. Preserve every unlisted field, do not "
    "add unrelated improvements, and do not generate a map. Return only valid JSON."
)


def build_ha_revision_plan_messages(context):
    context = context or {}
    adjustment_text = normalize_prompt_text(context.get("adjustmentText"))
    previous_plan = context.get("previousLevelPlan") or {}
    corridor_validation = context.get("corridorValidation") or {}
    regeneration_attempt = normalize_prompt_int(context.get("regenerationAttempt"))
    previous_options = normalize_previous_expansion_options(
        context.get("previousOptions")
    )
    previous_options_text = (
        json.dumps(previous_options, ensure_ascii=False, separators=(",", ":"))
        if previous_options
        else "none"
    )
    user_prompt = (
        f'Human adjustment: "{adjustment_text}". '
        "Previous LevelDesignPlan JSON: "
        + json.dumps(previous_plan, ensure_ascii=False, separators=(",", ":"))
        + ". Corridor verification JSON: "
        + json.dumps(corridor_validation, ensure_ascii=False, separators=(",", ":"))
        + f". Regeneration attempt: {regeneration_attempt}. "
        + f"Previously shown options: {previous_options_text}. "
        "Create exactly three alternatives that implement the same human intent "
        "through meaningfully different supported strategies. When regenerating, "
        "do not repeat a previously shown contract or merely rename it. Each visible "
        "description must state the concrete changes, what important structure stays "
        "unchanged, and the expected gameplay tradeoff in two or three concise "
        "English sentences, regardless of the human adjustment's language. "
        "Each option must use this shape: "
        '{"id":"A","title":"...","description":"...",'
        '"promptText":"{\\"changes\\":{...},\\"preserveUnlisted\\":true}"}. '
        "promptText is hidden from the player and must itself be a valid compact JSON "
        "string. changes must be a non-empty object containing only LevelDesignPlan "
        "fields that truly need to change. Allowed integer fields and ranges are: "
        "minSolutionSteps 18-30, maxSolutionSteps 32-50, minWaterAreas 1-2 and "
        "maxWaterAreas 1-2 unless explicit removal requires both 0, "
        "minWallObstacleBlocks exactly 2 (never 3) and maxWallObstacleBlocks 2-3 unless explicit "
        "removal requires both 0, "
        "minPushes 8-16, maxPushes 14-28, minReversePulls 14-24, "
        "maxReversePulls 24-40, corridorWidth 0-2. Allowed string fields are style, "
        "archetype, targetLayout, obstacleStyle, waterStyle, corridorPlacement, "
        "corridorOrientation, corridorRole, corridorPriority. archetype must be "
        "goal_room, bottleneck_corridor, split_route, or open_workshop; targetLayout "
        "must be clustered, split_pair, or edge_cluster; obstacleStyle must be "
        "central_baffle, side_choke, or goal_guard; waterStyle must be corner_pool, "
        "side_pool, or route_divider; corridorPlacement must be none, center, or side; "
        "corridorOrientation must be horizontal, vertical, or any; corridorRole must "
        "be visual_only, player_route, or required_box_route; corridorPriority must "
        "be preferred or required. Never include designNote in changes. "
        "preserveUnlisted must be true. Range minima must not exceed maxima. A none "
        "corridor requires width 0, orientation any, role visual_only, and priority "
        "preferred. When any corridor field changes, include every corridor field "
        "needed to keep that tuple internally consistent. Only introduce zero water "
        "or zero internal walls when the human "
        "explicitly requests their removal; otherwise preserve existing zero values "
        "but do not newly create them. Return exactly "
        '{"options":[optionA,optionB,optionC]}.'
    )
    return [
        {"role": "system", "content": HA_REVISION_PLAN_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def build_ha_revision_plan_edit_messages(context):
    context = context or {}
    adjustment_text = normalize_prompt_text(context.get("adjustmentText"))
    edited_description = normalize_prompt_text(context.get("editedDescription"))
    previous_plan = context.get("previousLevelPlan") or {}
    corridor_validation = context.get("corridorValidation") or {}
    original_option = context.get("originalOption") or {}
    user_prompt = (
        f'Original human adjustment: "{adjustment_text}". '
        f'Player-edited option description: "{edited_description}". '
        "Previous LevelDesignPlan JSON: "
        + json.dumps(previous_plan, ensure_ascii=False, separators=(",", ":"))
        + ". Corridor verification JSON: "
        + json.dumps(corridor_validation, ensure_ascii=False, separators=(",", ":"))
        + ". Original selected option JSON: "
        + json.dumps(original_option, ensure_ascii=False, separators=(",", ":"))
        + ". Treat the edited description as the final revision intent. Return a "
        "normalized English player-facing description that preserves "
        "the edit while expressing only supported observable Sokoban changes, plus "
        "a hidden implementation contract. The new contract must differ from the "
        "original option contract and change only the smallest coherent set of "
        "necessary fields. Return exactly "
        '{"description":"...","promptText":"{\\"changes\\":{...},'
        '\\"preserveUnlisted\\":true}"}. '
        "promptText must itself be valid compact JSON. changes must be non-empty and "
        "may contain only these integer fields: minSolutionSteps 18-30, "
        "maxSolutionSteps 32-50, minWaterAreas 1-2 and maxWaterAreas 1-2 unless the "
        "player explicitly requests no water, minWallObstacleBlocks exactly 2 (never 3) and "
        "maxWallObstacleBlocks 2-3 unless the player explicitly requests no internal "
        "walls, minPushes 8-16, maxPushes 14-28, minReversePulls 14-24, "
        "maxReversePulls 24-40, corridorWidth 0-2. Supported string fields are style, "
        "archetype, targetLayout, obstacleStyle, waterStyle, corridorPlacement, "
        "corridorOrientation, corridorRole, and corridorPriority. Never include "
        "designNote. Supported enums are: archetype goal_room, bottleneck_corridor, "
        "split_route, open_workshop; targetLayout clustered, split_pair, edge_cluster; "
        "obstacleStyle central_baffle, side_choke, goal_guard; waterStyle corner_pool, "
        "side_pool, route_divider; corridorPlacement none, center, side; "
        "corridorOrientation horizontal, vertical, any; corridorRole visual_only, "
        "player_route, required_box_route; corridorPriority preferred, required. "
        "preserveUnlisted must be true. Range minima must not exceed maxima. A none "
        "corridor requires width 0, orientation any, role visual_only, and priority "
        "preferred. If any corridor field changes, include the complete consistent "
        "corridor tuple. Do not introduce zero water or zero internal walls unless "
        "the player's edit explicitly requests removal. Silently verify that the "
        "normalized description and promptText describe the same implementation."
    )
    return [
        {"role": "system", "content": HA_REVISION_PLAN_EDIT_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


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
        + GENERATOR_CAPABILITY_CONTRACT
        + "All three options must clearly preserve the adapted, supported form of "
        "the original idea. Do not use "
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
        "timers, moving parts, or visual-only themes. Write all returned text in "
        "English even when the player's input uses another language. Return exactly "
        "this JSON shape: "
        "{\"options\":["
        "{\"id\":\"A\",\"title\":\"...\",\"description\":\"...\",\"promptText\":\"...\"},"
        "{\"id\":\"B\",\"title\":\"...\",\"description\":\"...\",\"promptText\":\"...\"},"
        "{\"id\":\"C\",\"title\":\"...\",\"description\":\"...\",\"promptText\":\"...\"}"
        "]}. "
        "title must be short and tailored to this idea. description must contain "
        "one or two concrete player-facing sentences when no adaptation is needed, "
        "or two or three sentences when adaptation is needed. It may mention only "
        "features that can actually appear on the generated board. Whenever any "
        "literal part of the original idea is removed, replaced, simplified, or "
        "represented indirectly, the FIRST sentence of description is mandatory "
        "and must use the English format 'Adaptation: convert X into Y'. This first "
        "sentence must "
        "name both the changed source element and its supported replacement. The "
        "source side may name the unsupported mechanic, but the replacement side "
        "must name only static supported pressure; it must not claim that the "
        "replacement still opens, closes, activates, triggers, transports, patrols, "
        "or otherwise performs the original behavior. Omit "
        "it only when every described part of the original idea is directly "
        "supported without reinterpretation. The remaining sentence or sentences "
        "must describe only the realizable result, never the unsupported element "
        "as if it remains a mechanic. Outside the adaptation sentence, do not say "
        "that the board simulates, resembles, or acts like the removed mechanic. "
        "Never claim that standing on a position activates something, that a box "
        "remotely opens or closes a route, that walls or water change during play, "
        "or that the player or a box crosses water. Normal physical blocking or "
        "vacating of the box's own floor position is allowed. "
        "promptText is hidden from the player and is an implementation contract "
        "for the downstream blueprint generator, not another creative-writing "
        "prompt. Keep promptText below 380 characters and use exactly this compact "
        "semicolon-separated format, preserving the ASCII keys, ASCII equals signs, "
        "ASCII semicolons, ASCII slashes, and enum tokens. All prose values must also "
        "be English ASCII text: "
        "core=<shared core experience>; archetype=<value>; targetLayout=<value>; "
        "obstacleStyle=<value>; waterStyle=<value>; water=<default|none>; "
        "walls=<default|none>; corridor=<placement>/<width>/<orientation>/<role>/"
        "<priority>; boxOrder=<concrete pressure>. "
        "archetype must be exactly one of goal_room, bottleneck_corridor, split_route, "
        "open_workshop. targetLayout must be exactly one of clustered, split_pair, "
        "edge_cluster. obstacleStyle must be exactly one of central_baffle, "
        "side_choke, goal_guard. waterStyle must be exactly one of corner_pool, "
        "side_pool, route_divider. corridor placement must be none, center, or side; "
        "width must be 0 for none and otherwise 1 or 2; orientation must be "
        "horizontal, vertical, or any; role must be visual_only, player_route, or "
        "required_box_route; priority must be preferred or required. Never invent "
        "an archetype or other enum token, and never use none for archetype, "
        "targetLayout, obstacleStyle, or waterStyle. Use required_box_route only "
        "when the "
        "user explicitly requires a box to pass through the corridor. If water=none, "
        "do not describe water; waterStyle remains an unused schema preference. If "
        "walls=none, disable the corridor as none/0/any/visual_only/preferred and "
        "do not describe internal walls; obstacleStyle remains an unused schema "
        "preference. Use water=none ONLY when the original user explicitly asks for "
        "no water, and use walls=none ONLY when the original user explicitly asks "
        "for no internal walls. Never choose either none value merely to create "
        "variety. Otherwise every option must use water=default and walls=default. "
        "The visible description must match the chosen tokens exactly: clustered "
        "means close goals, split_pair means separated goals, and edge_cluster means "
        "goals near an edge; central_baffle means a central wall obstacle, side_choke "
        "means a side restriction, and goal_guard means an obstacle near the goals; "
        "corner_pool means corner water, side_pool means side water, and route_divider "
        "means water dividing routes. A described corridor must match all five "
        "corridor tokens, and corridor=none must not be described as a corridor. "
        "After any adaptation sentence, the realizable description MUST explicitly "
        "cover every implementation choice in promptText: where the targets are "
        "placed according to targetLayout; where and how the obstacleStyle applies "
        "when walls=default; where the chosen waterStyle appears when water=default; "
        "the corridor placement, numeric width, and orientation when corridor is not "
        "none (or no corridor claim when it is none); and the concrete boxOrder "
        "pressure. Keep this coverage concise, but do not omit a selected feature. "
        "For example, "
        "water=none must still pair with a legal unused value such as "
        "waterStyle=corner_pool, never waterStyle=none. A valid compact contract is: "
        "core=route choice; archetype=split_route; targetLayout=split_pair; "
        "obstacleStyle=central_baffle; waterStyle=corner_pool; water=none; "
        "walls=default; corridor=none/0/any/visual_only/preferred; "
        "boxOrder=preserve the second box route. Do not "
        "reintroduce an unsupported mechanic in promptText. Use the same core value "
        "in all three options. Make each pair of options differ in at least two "
        "realized structure choices among archetype, targetLayout, obstacleStyle, "
        "waterStyle, and corridor. Before returning JSON, silently audit the exact "
        "format and allowed values, then compare every promptText token against the "
        "visible description and correct any mismatch before returning. Finally "
        "check that each title, description, and promptText describes the same "
        "supported implementation. Treat the draft as invalid and rewrite it before "
        "returning if ANY of these is true: an enum token is outside its allowed "
        "list; description promises exact coordinates, symmetry, exact alignment, "
        "or a literal T-, L-, or S-shaped wall; corridor placement is not none but "
        "description does not explicitly state its center-or-side placement, width, "
        "and horizontal-or-vertical orientation; corridor placement is none but "
        "description claims a corridor, passage, or hallway; "
        "waterStyle or targetLayout contradicts the visible placement description; "
        "any target, obstacle, water, corridor, or box-order contract choice is not "
        "represented in the visible description; "
        "water or walls is none without an explicit matching user request; or any "
        "unsupported mechanic appears after the adaptation sentence."
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


def build_prioritized_creative_context_prompt(
    creative_context,
    feature_constraints=None,
):
    creative_context = creative_context or {}
    feature_constraints = feature_constraints or {}
    idea_text = normalize_prompt_text(creative_context.get("ideaText"))
    original_idea = normalize_prompt_text(creative_context.get("originalIdeaText"))
    selected_direction = normalize_prompt_text(creative_context.get("selectedDirectionText"))
    refinement_feedback = normalize_prompt_text(creative_context.get("refinementFeedbackText"))
    adjustment_history = normalize_prompt_text(creative_context.get("adjustmentHistoryText"))
    latest_adjustment = normalize_prompt_text(creative_context.get("latestAdjustmentText"))
    revision_mode = normalize_prompt_text(
        creative_context.get("revisionMode")
    ).lower()
    previous_plan = normalize_prompt_text(creative_context.get("previousLevelPlan"))
    previous_metrics = normalize_prompt_text(
        creative_context.get("previousLevelMetrics")
    )
    selected_ha_plan = normalize_prompt_text(
        creative_context.get("selectedHAPlan")
    )
    style_description = normalize_prompt_text(
        creative_context.get("styleDescription")
    )
    parts = []

    if revision_mode == "human":
        parts.append(
            "Revision authority mode: HUMAN-led. The user owns the diagnosis, "
            "revision proposal, and final decision. The model is only a constraint "
            "translator. Follow this priority order: solvability and supported "
            "Sokoban rules; the user's explicit latest revision instruction; every "
            "unchanged field in the previous plan; selected design direction and "
            "original idea; earlier context; diversity. Do not diagnose additional "
            "problems, optimize unrelated qualities, compensate beyond the request, "
            "or introduce an unrequested structural change. "
        )
    elif revision_mode == "ai":
        parts.append(
            "Revision authority mode: AI-led. The user supplies evaluative feedback, "
            "while the model owns the diagnosis, revision proposal, and revision "
            "decision. Follow this priority order: solvability, supported Sokoban "
            "rules, and explicit user prohibitions; the original idea and the core "
            "experience of the selected direction; the problem indicated by the "
            "latest feedback; evidence from the previous plan and level metrics; a "
            "small coherent set of causally relevant changes; diversity. Preserve "
            "the creative core, but choose the concrete supported blueprint fields "
            "without requiring the user to name them. Avoid arbitrary or unrelated "
            "changes. "
        )
    elif revision_mode == "ha":
        parts.append(
            "Revision authority mode: HUMAN-AI collaborative. The human owns the "
            "revision intent and the final choice; the AI proposed implementation "
            "alternatives. Follow this priority order: solvability and supported "
            "Sokoban rules; explicit human prohibitions and requested outcome; the "
            "selected HA revision plan; preservation of every unlisted field in the "
            "previous blueprint; the core experience of the original selected "
            "direction. Do not add an unselected optimization. "
        )
    else:
        parts.append(
            "Follow this priority order: solvability and supported Sokoban rules; "
            "latest user adjustment; selected design direction; original user idea; "
            "earlier adjustments and refinement feedback; general difficulty and "
            "quality preferences; variation and recent-blueprint diversity. "
        )

    if style_description:
        parts.append(
            f'Description-to-Level style request: "{style_description[:420]}". '
            "Use it to shape the observable spatial experience and descriptive "
            "style while respecting every explicit generation parameter. "
            "Translate unsupported or purely visual ideas into supported Sokoban "
            "route pressure, target placement, wall obstacles, water placement, "
            "standing-space pressure, or box order. "
        )

    if latest_adjustment:
        if revision_mode == "ai":
            parts.append(
                f'Latest user evaluation (diagnostic evidence, not a field-level '
                f'specification): "{latest_adjustment}". Infer the most likely '
                "supported level-design cause, then change only the fields that "
                "coherently address that diagnosis. "
            )
        else:
            parts.append(
                f'Latest user adjustment (authoritative hard requirement): "{latest_adjustment}". '
                "When it conflicts with earlier intent, follow the latest adjustment "
                "while preserving as much earlier intent as possible. "
            )

    if previous_plan and revision_mode in ("human", "ai", "ha"):
        parts.append(f'Previous applied blueprint JSON: "{previous_plan}". ')

    if previous_metrics and revision_mode == "ai":
        parts.append(
            f'Previous level diagnostic metrics JSON: "{previous_metrics}". Use '
            "solver steps and pushes as structural difficulty evidence, and player "
            "moves, pushes, restarts, and completion time as behavioral evidence. "
            "Do not treat search cost or generation effort as player difficulty. "
        )

    if selected_direction:
        if revision_mode in ("ai", "ha"):
            parts.append(
                f'Selected design direction: "{selected_direction}". Preserve its '
                "core experience and all explicit user prohibitions. Exact structural "
                "fields may change only when the diagnosis requires them; preserve "
                "all unrelated fields from the previous plan. "
            )
        else:
            parts.append(
                f'Selected design direction: "{selected_direction}". This direction '
                "has already been shown to and selected by the user. Treat every "
                "supported structural statement in it as an implementation requirement, "
                "not as inspiration. Do not reinterpret, embellish, or replace it with "
                "a different design direction. Parse its compact key=value contract. "
                "Copy archetype, targetLayout, obstacleStyle, and waterStyle into the "
                "same-named JSON fields exactly, without synonyms. Parse corridor as "
                "placement/width/orientation/role/priority and copy those five values "
                "into the corresponding corridor JSON fields exactly. water=none means "
                "minWaterAreas=0 and maxWaterAreas=0; walls=none means both wall-obstacle "
                "counts are 0 and all corridor fields are disabled. Preserve core and "
                "boxOrder through the supported structural fields and designNote rather "
                "than inventing a new mechanic. Only the latest user adjustment or an "
                "explicit zero-feature constraint may override the contract. If a latest "
                "adjustment conflicts, change only the fields required by that adjustment "
                "and preserve every other selected value. Before returning JSON, silently "
                "compare every selected contract value with the final JSON and correct "
                "any mismatch. "
            )

    if selected_ha_plan and revision_mode == "ha":
        parts.append(
            f'Selected HA revision option JSON: "{selected_ha_plan}". Its hidden '
            "changes contract is authoritative. Apply exactly those changed fields "
            "to the previous blueprint and preserve every unlisted field. "
        )

    if original_idea:
        parts.append(f'Original user idea: "{original_idea}". ')

    if adjustment_history:
        parts.append(f'Earlier adjustment history (context only): "{adjustment_history}". ')

    if refinement_feedback:
        parts.append(f'Refinement feedback (context only): "{refinement_feedback}". ')

    if idea_text and not any((original_idea, selected_direction, latest_adjustment)):
        parts.append(f'Legacy combined user idea: "{idea_text}". ')

    if feature_constraints.get("noInternalWalls"):
        parts.append(
            "The explicit no-internal-walls requirement overrides every corridor "
            "request in the user context. Keep all corridor fields disabled. "
        )
    else:
        parts.append(
            "Do not satisfy a structural request only through style or designNote; "
            "encode it in the corridor fields. A request for a narrow corridor in "
            "the map center must use archetype=bottleneck_corridor, "
            "obstacleStyle=central_baffle, corridorPlacement=center, "
            "corridorPriority=required, and corridorWidth=1 unless another width is "
            "explicitly requested. Use corridorRole=required_box_route only when the "
            "user says a box must pass through it; otherwise use player_route. "
        )

    if revision_mode == "human":
        parts.append(
            "Use the previous blueprint as the baseline and apply the minimum field "
            "changes required by the user. designNote must begin with 'User-directed "
            "revision:' and briefly state only the translated user decision. If an "
            "evaluation-only message reaches this stage, do not invent a solution; "
            "copy the previous plan and use 'Human clarification required' in designNote. "
        )
    elif revision_mode == "ai":
        parts.append(
            "Before returning JSON, form a concise evidence-based diagnosis and choose "
            "one small coherent revision strategy. designNote must use the format "
            "'AI diagnosis: <cause>; revision: <chosen changes>'. Do not merely restate "
            "the user's evaluation. "
        )
    elif revision_mode == "ha":
        parts.append(
            "designNote must begin with 'Human-AI revision:' and summarize the "
            "human request and the selected implementation plan. Do not substitute "
            "one of the unselected alternatives. "
        )
    return "".join(parts)


def build_human_adjustment_clarity_messages(adjustment_text):
    clean_text = normalize_prompt_text(adjustment_text)
    return [
        {
            "role": "system",
            "content": HUMAN_ADJUSTMENT_CLARITY_SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": f'Evaluate this Human-led revision instruction: "{clean_text}"',
        },
    ]


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
