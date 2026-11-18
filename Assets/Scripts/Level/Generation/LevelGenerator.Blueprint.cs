using System;
using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public partial class LevelGenerator
{
    public void ApplyPlan(LevelDesignPlan plan)
    {
        if (plan == null)
        {
            return;
        }

        ResolveReferences();

        if (rules == null)
        {
            Debug.LogWarning("LevelGenerator: Cannot apply level design plan because rules are missing.");
            return;
        }

        rules.minSolutionSteps = Mathf.Max(0, plan.minSolutionSteps);
        rules.maxSolutionSteps = Mathf.Max(rules.minSolutionSteps, plan.maxSolutionSteps);
        if (plan.minPushes > 0 || plan.maxPushes > 0)
        {
            rules.minPushes = Mathf.Max(0, plan.minPushes);
            rules.maxPushes = Mathf.Max(rules.minPushes, plan.maxPushes);
        }

        rules.minWaterAreas = Mathf.Max(0, plan.minWaterAreas);
        rules.maxWaterAreas = Mathf.Max(rules.minWaterAreas, plan.maxWaterAreas);
        rules.minWallObstacleBlocks = Mathf.Max(0, plan.minWallObstacleBlocks);
        rules.maxWallObstacleBlocks = Mathf.Max(rules.minWallObstacleBlocks, plan.maxWallObstacleBlocks);
        preserveZeroWaterConstraint = rules.maxWaterAreas == 0;
        preserveZeroInternalWallConstraint = rules.maxWallObstacleBlocks == 0;

        if (plan.minReversePulls > 0 || plan.maxReversePulls > 0)
        {
            rules.minReversePulls = Mathf.Max(0, plan.minReversePulls);
            rules.maxReversePulls = Mathf.Max(rules.minReversePulls, plan.maxReversePulls);
        }

        currentArchetype = NormalizeArchetype(plan.archetype);
        currentTargetLayout = NormalizeTargetLayout(plan.targetLayout);
        currentObstacleStyle = NormalizeObstacleStyle(plan.obstacleStyle);
        currentWaterStyle = NormalizeWaterStyle(plan.waterStyle);
        currentDesignNote = string.IsNullOrEmpty(plan.designNote) ? "" : plan.designNote;
        currentCorridorPlacement = NormalizeCorridorPlacement(plan.corridorPlacement);
        currentCorridorWidth = currentCorridorPlacement == "none"
            ? 0
            : Mathf.Clamp(plan.corridorWidth, 1, 2);
        currentCorridorOrientation = NormalizeCorridorOrientation(plan.corridorOrientation);
        currentCorridorRole = NormalizeCorridorRole(plan.corridorRole);
        currentCorridorPriority = NormalizeCorridorPriority(plan.corridorPriority);
        currentStructureTemplate = LevelGenerationTemplates.GetStructureTemplate(currentArchetype);
        hasDesignBlueprint = true;
        activeLLMQualityGate = true;

        rules.maxGenerateAttempts = Mathf.Min(rules.maxGenerateAttempts, 450);
        rules.maxReverseStepAttempts = Mathf.Min(rules.maxReverseStepAttempts, 320);

        if (levelSolver != null)
        {
            levelSolver.maxSearchStates = Mathf.Min(levelSolver.maxSearchStates, 120000);
        }

        if (logGenerationResult)
        {
            Debug.Log(
                "LevelGenerator applied level design plan:"
                + " solutionSteps=" + rules.minSolutionSteps + "-" + rules.maxSolutionSteps
                + ", pushes=" + rules.minPushes + "-" + rules.maxPushes
                + ", waterAreas=" + rules.minWaterAreas + "-" + rules.maxWaterAreas
                + ", wallObstacleBlocks=" + rules.minWallObstacleBlocks + "-" + rules.maxWallObstacleBlocks
                + ", reversePulls=" + rules.minReversePulls + "-" + rules.maxReversePulls
                + ", archetype=" + currentArchetype
                + ", targetLayout=" + currentTargetLayout
                + ", obstacleStyle=" + currentObstacleStyle
                + ", waterStyle=" + currentWaterStyle
                + ", style=" + plan.style
                + ", designNote=" + currentDesignNote
                + ", corridor=" + currentCorridorPlacement
                + "/" + currentCorridorWidth
                + "/" + currentCorridorOrientation
                + "/" + currentCorridorRole
                + "/" + currentCorridorPriority
            );
        }
    }

    private void ApplyAlgorithmDesignBlueprint()
    {
        LevelGenerationTemplates.StructureTemplate[] templates = LevelGenerationTemplates.StructureTemplates;

        if (templates == null || templates.Length == 0)
        {
            currentArchetype = DefaultArchetype;
            currentTargetLayout = DefaultTargetLayout;
            currentObstacleStyle = DefaultObstacleStyle;
            currentWaterStyle = DefaultWaterStyle;
            currentStructureTemplate = LevelGenerationTemplates.GetStructureTemplate(DefaultArchetype);
            return;
        }

        EnsureAlgorithmTemplateOrder(templates);

        int orderIndex = algorithmTemplateCursor % algorithmTemplateOrder.Length;
        int templateIndex = algorithmTemplateOrder[orderIndex];
        algorithmTemplateCursor++;
        currentStructureTemplate = templates[templateIndex];
        currentArchetype = currentStructureTemplate.archetype;
        currentTargetLayout = GetAlgorithmTargetLayout(currentArchetype);
        currentObstacleStyle = GetAlgorithmObstacleStyle(currentArchetype);
        currentWaterStyle = GetAlgorithmWaterStyle(currentArchetype);
        currentDesignNote = "Algorithm template variation";
        hasDesignBlueprint = true;
        activeLLMQualityGate = false;
    }

    private void EnsureAlgorithmTemplateOrder(LevelGenerationTemplates.StructureTemplate[] templates)
    {
        if (!algorithmTemplateOrderNeedsShuffle
            && algorithmTemplateOrder != null
            && algorithmTemplateOrder.Length == templates.Length
            && algorithmTemplateCursor < algorithmTemplateOrder.Length)
        {
            return;
        }

        int seed = rules != null && rules.useFixedSeed
            ? rules.seed + algorithmTemplateRoundCounter
            : GetRuntimeSeed();
        System.Random shuffleRandom = new System.Random(seed);
        algorithmTemplateOrder = BuildShuffledAlgorithmTemplateOrder(templates, shuffleRandom);
        algorithmTemplateCursor = 0;
        algorithmTemplateOrderNeedsShuffle = false;

        if (logGenerationResult)
        {
            Debug.Log(
                "LevelGenerator shuffled algorithm template order:"
                + " roundCounter=" + algorithmTemplateRoundCounter
                + ", seed=" + seed
                + ", order=" + string.Join(",", algorithmTemplateOrder)
            );
        }
    }

    private static int[] BuildShuffledAlgorithmTemplateOrder(
        LevelGenerationTemplates.StructureTemplate[] templates,
        System.Random shuffleRandom)
    {
        int[] bestOrder = null;
        int bestAdjacentMatchCount = int.MaxValue;
        int attemptCount = Mathf.Max(8, templates.Length * 2);

        for (int attempt = 0; attempt < attemptCount; attempt++)
        {
            int[] order = CreateSequentialTemplateOrder(templates.Length);
            ShuffleTemplateOrder(order, shuffleRandom);
            int adjacentMatchCount = CountAdjacentArchetypeMatches(order, templates);

            if (adjacentMatchCount < bestAdjacentMatchCount)
            {
                bestOrder = order;
                bestAdjacentMatchCount = adjacentMatchCount;

                if (bestAdjacentMatchCount == 0)
                {
                    break;
                }
            }
        }

        return bestOrder ?? CreateSequentialTemplateOrder(templates.Length);
    }

    private static int[] CreateSequentialTemplateOrder(int count)
    {
        int[] order = new int[count];

        for (int i = 0; i < count; i++)
        {
            order[i] = i;
        }

        return order;
    }

    private static void ShuffleTemplateOrder(int[] order, System.Random shuffleRandom)
    {
        for (int i = order.Length - 1; i > 0; i--)
        {
            int swapIndex = shuffleRandom.Next(i + 1);
            int value = order[i];
            order[i] = order[swapIndex];
            order[swapIndex] = value;
        }
    }

    private static int CountAdjacentArchetypeMatches(
        int[] order,
        LevelGenerationTemplates.StructureTemplate[] templates)
    {
        int count = 0;

        for (int i = 1; i < order.Length; i++)
        {
            if (templates[order[i - 1]].archetype == templates[order[i]].archetype)
            {
                count++;
            }
        }

        return count;
    }

    private string GetAlgorithmTargetLayout(string archetype)
    {
        if (archetype == "goal_room")
        {
            return random.Next(3) == 0 ? "edge_cluster" : "clustered";
        }

        if (archetype == "bottleneck_corridor")
        {
            return random.Next(2) == 0 ? "split_pair" : "edge_cluster";
        }

        if (archetype == "split_route")
        {
            return "split_pair";
        }

        return random.Next(2) == 0 ? "split_pair" : "clustered";
    }

    private string GetAlgorithmObstacleStyle(string archetype)
    {
        if (archetype == "goal_room")
        {
            return "goal_guard";
        }

        if (archetype == "bottleneck_corridor")
        {
            return "side_choke";
        }

        if (archetype == "split_route")
        {
            return "central_baffle";
        }

        return random.Next(2) == 0 ? "central_baffle" : "side_choke";
    }

    private string GetAlgorithmWaterStyle(string archetype)
    {
        if (archetype == "goal_room")
        {
            return random.Next(2) == 0 ? "corner_pool" : "side_pool";
        }

        if (archetype == "bottleneck_corridor")
        {
            return random.Next(2) == 0 ? "side_pool" : "route_divider";
        }

        if (archetype == "split_route")
        {
            return "route_divider";
        }

        return random.Next(2) == 0 ? "side_pool" : "corner_pool";
    }

    private string NormalizeArchetype(string value)
    {
        value = NormalizeBlueprintValue(value);

        if (value == "goal_room"
            || value == "bottleneck_corridor"
            || value == "split_route"
            || value == "open_workshop")
        {
            return value;
        }

        return DefaultArchetype;
    }

    private string NormalizeTargetLayout(string value)
    {
        value = NormalizeBlueprintValue(value);

        if (value == "clustered" || value == "split_pair" || value == "edge_cluster")
        {
            return value;
        }

        return DefaultTargetLayout;
    }

    private string NormalizeObstacleStyle(string value)
    {
        value = NormalizeBlueprintValue(value);

        if (value == "central_baffle" || value == "side_choke" || value == "goal_guard")
        {
            return value;
        }

        return DefaultObstacleStyle;
    }

    private string NormalizeWaterStyle(string value)
    {
        value = NormalizeBlueprintValue(value);

        if (value == "corner_pool" || value == "side_pool" || value == "route_divider")
        {
            return value;
        }

        return DefaultWaterStyle;
    }

    private string NormalizeCorridorPlacement(string value)
    {
        value = NormalizeBlueprintValue(value);
        return value == "center" || value == "side" ? value : "none";
    }

    private string NormalizeCorridorOrientation(string value)
    {
        value = NormalizeBlueprintValue(value);
        return value == "horizontal" || value == "vertical" ? value : "any";
    }

    private string NormalizeCorridorRole(string value)
    {
        value = NormalizeBlueprintValue(value);

        if (value == "player_route" || value == "required_box_route")
        {
            return value;
        }

        return "visual_only";
    }

    private string NormalizeCorridorPriority(string value)
    {
        return NormalizeBlueprintValue(value) == "required" ? "required" : "preferred";
    }

    private string NormalizeBlueprintValue(string value)
    {
        return string.IsNullOrEmpty(value) ? "" : value.Trim().ToLowerInvariant();
    }
}
