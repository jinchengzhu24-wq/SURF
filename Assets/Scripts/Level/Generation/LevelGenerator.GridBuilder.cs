using System;
using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public partial class LevelGenerator
{
    private bool TryCreateCandidate(
        out string[] rows,
        out int reversePulls,
        out CandidateFailureReason failureReason)
    {
        rows = null;
        reversePulls = 0;
        failureReason = CandidateFailureReason.None;
        if (!TryCreateBaseGrid(out char[,] grid))
        {
            failureReason = CandidateFailureReason.BaseGrid;
            return false;
        }

        bool placedTargets = false;

        if (hasDesignBlueprint)
        {
            if (!TryAddWallObstacleBlocks(grid))
            {
                failureReason = CandidateFailureReason.WallObstacles;
                return false;
            }

            if (!TryAddWaterAreas(grid))
            {
                failureReason = CandidateFailureReason.WaterAreas;
                return false;
            }
        }
        else
        {
            if (!TryAddWaterAreas(grid))
            {
                failureReason = CandidateFailureReason.WaterAreas;
                return false;
            }

            if (!TryPlaceTargets(grid))
            {
                failureReason = CandidateFailureReason.Targets;
                return false;
            }

            placedTargets = true;

            if (!TryAddWallObstacleBlocks(grid))
            {
                failureReason = CandidateFailureReason.WallObstacles;
                return false;
            }
        }

        if (!placedTargets && !TryPlaceTargets(grid))
        {
            failureReason = CandidateFailureReason.Targets;
            return false;
        }

        if (!TryPlacePlayer(grid))
        {
            failureReason = CandidateFailureReason.Player;
            return false;
        }

        int targetPulls = NextInclusive(GetEffectiveMinReversePulls(), GetEffectiveMaxReversePulls());

        if (!TryReversePulls(grid, targetPulls, out reversePulls))
        {
            failureReason = CandidateFailureReason.ReversePulls;
            return false;
        }

        if (HasAdjacentBoxTargetPair())
        {
            if (logGenerationResult)
            {
                Debug.LogWarning("LevelGenerator: Rejected candidate with adjacent box start and target.");
            }

            failureReason = CandidateFailureReason.TileRules;
            return false;
        }

        if (!ValidateTileRules(grid, true))
        {
            failureReason = CandidateFailureReason.TileRules;
            return false;
        }

        if (!TryBuildRows(grid, out rows))
        {
            failureReason = CandidateFailureReason.BuildRows;
            return false;
        }

        return true;
    }

    private bool TryCreateBaseGrid(out char[,] grid)
    {
        candidateCorridorOpeningCells.Clear();

        if (rules.width != 12 || rules.height != 10)
        {
            grid = null;
            return false;
        }

        bool needsHardCorridor = currentCorridorPlacement != "none";
        grid = CreateSupportedOuterShell();

        if (needsHardCorridor && !TryApplyCorridorTemplate(grid))
        {
            return false;
        }

        return HasEnoughGroundCells(grid)
            && AreGroundCellsConnected(grid)
            && ValidateWallTileRules(grid);
    }

    private bool TryApplyCorridorTemplate(char[,] grid)
    {
        if (grid == null || currentCorridorWidth < 1 || currentCorridorWidth > 2)
        {
            return false;
        }

        candidateCorridorOrientation = currentCorridorOrientation == "any"
            ? (random.Next(2) == 0 ? "horizontal" : "vertical")
            : currentCorridorOrientation;

        if (candidateCorridorOrientation == "vertical")
        {
            int dividerX = currentCorridorPlacement == "center"
                ? rules.width / 2
                : (random.Next(2) == 0 ? 3 : rules.width - 4);
            int openingStartY = rules.height / 2 - (currentCorridorWidth - 1) / 2;

            for (int y = 1; y < rules.height - 1; y++)
            {
                Vector2Int position = new Vector2Int(dividerX, y);
                bool opening = y >= openingStartY && y < openingStartY + currentCorridorWidth;
                grid[dividerX, y] = opening ? Ground : Wall;

                if (opening)
                {
                    candidateCorridorOpeningCells.Add(position);
                }
            }

            int upperCapY = openingStartY - 1;
            int lowerCapY = openingStartY + currentCorridorWidth;
            grid[dividerX - 1, upperCapY] = Wall;
            grid[dividerX - 1, lowerCapY] = Wall;
        }
        else if (candidateCorridorOrientation == "horizontal")
        {
            int dividerY = currentCorridorPlacement == "center"
                ? rules.height / 2
                : (random.Next(2) == 0 ? 3 : rules.height - 4);
            int openingStartX = rules.width / 2 - (currentCorridorWidth - 1) / 2;

            for (int x = 1; x < rules.width - 1; x++)
            {
                Vector2Int position = new Vector2Int(x, dividerY);
                bool opening = x >= openingStartX && x < openingStartX + currentCorridorWidth;
                grid[x, dividerY] = opening ? Ground : Wall;

                if (opening)
                {
                    candidateCorridorOpeningCells.Add(position);
                }
            }

            int leftCapX = openingStartX - 1;
            int rightCapX = openingStartX + currentCorridorWidth;
            grid[leftCapX, dividerY - 1] = Wall;
            grid[rightCapX, dividerY - 1] = Wall;
        }
        else
        {
            return false;
        }

        return candidateCorridorOpeningCells.Count == currentCorridorWidth;
    }

    private CorridorValidationResult ValidateCorridorStructure(string[] rows)
    {
        CorridorValidationResult result = new CorridorValidationResult
        {
            requested = currentCorridorPlacement != "none",
            placement = currentCorridorPlacement,
            width = currentCorridorWidth,
            orientation = candidateCorridorOrientation,
            uniquePassage = currentCorridorPlacement == "none"
        };

        if (!result.requested || rows == null || candidateCorridorOpeningCells.Count == 0)
        {
            return result;
        }

        int passableCount = 0;
        bool adjacentAreasOpen = true;

        if (candidateCorridorOrientation == "vertical")
        {
            int dividerX = FirstCorridorOpening().x;

            for (int y = 1; y < rules.height - 1; y++)
            {
                Vector2Int position = new Vector2Int(dividerX, y);
                bool passable = IsCorridorPassable(rows, position);

                if (passable)
                {
                    passableCount++;
                }

                if (candidateCorridorOpeningCells.Contains(position))
                {
                    adjacentAreasOpen &= IsCorridorPassable(rows, position + Vector2Int.left)
                        && IsCorridorPassable(rows, position + Vector2Int.right);
                }
                else if (passable)
                {
                    adjacentAreasOpen = false;
                }
            }
        }
        else
        {
            int dividerY = FirstCorridorOpening().y;

            for (int x = 1; x < rules.width - 1; x++)
            {
                Vector2Int position = new Vector2Int(x, dividerY);
                bool passable = IsCorridorPassable(rows, position);

                if (passable)
                {
                    passableCount++;
                }

                if (candidateCorridorOpeningCells.Contains(position))
                {
                    adjacentAreasOpen &= IsCorridorPassable(rows, position + Vector2Int.up)
                        && IsCorridorPassable(rows, position + Vector2Int.down);
                }
                else if (passable)
                {
                    adjacentAreasOpen = false;
                }
            }
        }

        result.uniquePassage = adjacentAreasOpen && passableCount == currentCorridorWidth;
        return result;
    }

    private Vector2Int FirstCorridorOpening()
    {
        foreach (Vector2Int position in candidateCorridorOpeningCells)
        {
            return position;
        }

        return Vector2Int.zero;
    }

    private bool IsCorridorPassable(string[] rows, Vector2Int position)
    {
        char tile = LevelData.GetMapTile(rows, position);
        return tile == Ground || tile == Player || tile == Box || tile == Target;
    }

    private string GetCorridorFailureMessage(
        CorridorValidationResult validation,
        bool requiresBoxPassage)
    {
        if (!validation.uniquePassage)
        {
            return "The requested corridor position, width, or unique opening was not preserved.";
        }

        if (!validation.playerCanPass)
        {
            return "The requested corridor was not part of a solvable player route.";
        }

        if (requiresBoxPassage && !validation.boxPassedThrough)
        {
            return "No solved box route passed through the requested corridor.";
        }

        return "The requested corridor could not be verified.";
    }

    private bool HasRequiredCorridor()
    {
        return currentCorridorPlacement != "none" && currentCorridorPriority == "required";
    }

    private char[,] CreateSupportedOuterShell()
    {
        if (rules.width != 12 || rules.height != 10)
        {
            return CreateRectangularOuterShell();
        }

        string[] template = LevelGenerationTemplates.OuterShellTemplates[GetOuterShellTemplateIndex()];
        bool mirrorHorizontally = random.Next(2) == 0;
        char[,] grid = new char[rules.width, rules.height];

        for (int y = 0; y < rules.height; y++)
        {
            string row = template[y];

            for (int x = 0; x < rules.width; x++)
            {
                int sourceX = mirrorHorizontally ? rules.width - 1 - x : x;
                grid[x, y] = row[sourceX];
            }
        }

        return grid;
    }

    private int GetOuterShellTemplateIndex()
    {
        if (hasDesignBlueprint && currentStructureTemplate != null)
        {
            return currentStructureTemplate.outerShellTemplateIndex;
        }

        return random.Next(LevelGenerationTemplates.OuterShellTemplates.Length);
    }

    private char[,] CreateRectangularOuterShell()
    {
        char[,] grid = new char[rules.width, rules.height];

        for (int y = 0; y < rules.height; y++)
        {
            for (int x = 0; x < rules.width; x++)
            {
                bool border = x == 0 || y == 0 || x == rules.width - 1 || y == rules.height - 1;
                grid[x, y] = border ? Wall : Ground;
            }
        }

        return grid;
    }

    private bool TryAddWaterAreas(char[,] grid)
    {
        int waterAreaCount = NextInclusive(rules.minWaterAreas, rules.maxWaterAreas);
        int placedWaterAreas = 0;

        for (int i = 0; i < waterAreaCount; i++)
        {
            if (TryAddWaterArea(grid))
            {
                placedWaterAreas++;
            }
        }

        return placedWaterAreas >= rules.minWaterAreas
            && AreGroundCellsConnected(grid)
            && ValidateTileRules(grid);
    }

    private bool TryAddWaterArea(char[,] grid)
    {
        for (int attempt = 0; attempt < 20; attempt++)
        {
            Vector2Int size = new Vector2Int(
                NextInclusive(rules.minWaterWidth, rules.maxWaterWidth),
                NextInclusive(rules.minWaterHeight, rules.maxWaterHeight)
            );

            if (!rules.IsValidWaterRectSize(size.x, size.y))
            {
                continue;
            }

            List<Vector2Int> origins = GetWaterOriginCandidates(grid, size);

            if (origins.Count == 0)
            {
                continue;
            }

            Shuffle(origins);
            origins.Sort((left, right) => GetWaterOriginScore(right, size).CompareTo(GetWaterOriginScore(left, size)));

            for (int originIndex = 0; originIndex < origins.Count; originIndex++)
            {
                Vector2Int origin = origins[originIndex];

                for (int y = origin.y; y < origin.y + size.y; y++)
                {
                    for (int x = origin.x; x < origin.x + size.x; x++)
                    {
                        grid[x, y] = Water;
                    }
                }

                if (AreGroundCellsConnected(grid))
                {
                    return true;
                }

                for (int y = origin.y; y < origin.y + size.y; y++)
                {
                    for (int x = origin.x; x < origin.x + size.x; x++)
                    {
                        grid[x, y] = Ground;
                    }
                }
            }
        }

        return false;
    }

    private List<Vector2Int> GetWaterOriginCandidates(char[,] grid, Vector2Int size)
    {
        List<Vector2Int> origins = new List<Vector2Int>();
        int minOriginY = 2;
        int maxOriginX = rules.width - size.x - 1;
        int maxOriginY = rules.height - size.y - 1;

        if (maxOriginX < 1 || maxOriginY < minOriginY)
        {
            return origins;
        }

        for (int y = minOriginY; y <= maxOriginY; y++)
        {
            for (int x = 1; x <= maxOriginX; x++)
            {
                Vector2Int origin = new Vector2Int(x, y);

                if (rules.IsValidWaterRect(origin, size) && CanPlaceWaterRect(grid, origin, size))
                {
                    origins.Add(origin);
                }
            }
        }

        return origins;
    }

    private int GetWaterOriginScore(Vector2Int origin, Vector2Int size)
    {
        Vector2Int center = new Vector2Int(origin.x + size.x / 2, origin.y + size.y / 2);
        int score;

        if (currentWaterStyle == "corner_pool")
        {
            score = 100 - GetDistanceToNearestPlayableCorner(center) * 8;
            return score + GetAnchorScore(center, currentStructureTemplate.waterAnchors, 80);
        }

        if (currentWaterStyle == "route_divider")
        {
            Vector2Int mapCenter = GetMapCenter();
            score = 100 - ManhattanDistance(center, mapCenter) * 6;
            return score + GetAnchorScore(center, currentStructureTemplate.waterAnchors, 80);
        }

        int sideDistance = Mathf.Min(center.x - 1, rules.width - 2 - center.x);
        int verticalCenterDistance = Mathf.Abs(center.y - GetMapCenter().y);
        score = 100 - sideDistance * 8 - verticalCenterDistance * 2;
        return score + GetAnchorScore(center, currentStructureTemplate.waterAnchors, 80);
    }

    private bool TryAddWallObstacleBlocks(char[,] grid)
    {
        if (!HasEnoughGroundCells(grid) || !AreGroundCellsConnected(grid) || !ValidateWallTileRules(grid))
        {
            return false;
        }

        int obstacleCount = NextInclusive(rules.minWallObstacleBlocks, rules.maxWallObstacleBlocks);
        int placedObstacles = 0;
        int maxAttempts = Mathf.Max(80, obstacleCount * 50);

        if (RequiresLLMQualityGate() && placedObstacles < obstacleCount)
        {
            if (TryAddSurroundedWallObstacleBlock(grid))
            {
                placedObstacles++;
            }
        }

        if (hasDesignBlueprint && placedObstacles < obstacleCount)
        {
            if (TryAddTemplateWallObstacleBlock(grid))
            {
                placedObstacles++;
            }
        }

        for (int attempt = 0; attempt < maxAttempts && placedObstacles < obstacleCount; attempt++)
        {
            if (TryAddWallObstacleBlock(grid))
            {
                placedObstacles++;
            }
        }

        return placedObstacles >= obstacleCount;
    }

    private bool TryAddSurroundedWallObstacleBlock(char[,] grid)
    {
        string[] shape = { "#" };
        List<Vector2Int> origins = GetWallObstacleOriginCandidates(
            grid,
            shape,
            GetWallObstacleShapeSize(shape)
        );

        Shuffle(origins);
        origins.Sort((left, right) => GetWallObstacleOriginScore(right, Vector2Int.one)
            .CompareTo(GetWallObstacleOriginScore(left, Vector2Int.one)));

        for (int i = 0; i < origins.Count; i++)
        {
            if (TryCommitWallObstacleShape(grid, origins[i], shape))
            {
                return true;
            }
        }

        return false;
    }

    private bool TryAddWallObstacleBlock(char[,] grid)
    {
        string[][] availableShapes = LevelGenerationTemplates.WallObstacleShapes;
        string[] shape = availableShapes[random.Next(availableShapes.Length)];
        Vector2Int size = GetWallObstacleShapeSize(shape);
        List<Vector2Int> origins = GetWallObstacleOriginCandidates(grid, shape, size);

        if (origins.Count == 0)
        {
            return false;
        }

        Shuffle(origins);
        origins.Sort((left, right) => GetWallObstacleOriginScore(right, size).CompareTo(GetWallObstacleOriginScore(left, size)));

        for (int i = 0; i < origins.Count; i++)
        {
            if (TryCommitWallObstacleShape(grid, origins[i], shape))
            {
                return true;
            }
        }

        return false;
    }

    private bool TryAddTemplateWallObstacleBlock(char[,] grid)
    {
        List<int> shapeIndices = new List<int>(currentStructureTemplate.wallShapeIndices);
        Shuffle(shapeIndices);

        for (int shapeIndex = 0; shapeIndex < shapeIndices.Count; shapeIndex++)
        {
            string[] shape = GetWallObstacleShape(shapeIndices[shapeIndex]);
            Vector2Int size = GetWallObstacleShapeSize(shape);
            List<Vector2Int> origins = GetTemplateWallObstacleOrigins(size);

            Shuffle(origins);
            origins.Sort((left, right) => GetWallObstacleOriginScore(right, size).CompareTo(GetWallObstacleOriginScore(left, size)));

            for (int i = 0; i < origins.Count; i++)
            {
                if (TryCommitWallObstacleShape(grid, origins[i], shape))
                {
                    return true;
                }
            }
        }

        return false;
    }

    private string[] GetWallObstacleShape(int shapeIndex)
    {
        int index = Mathf.Abs(shapeIndex) % LevelGenerationTemplates.WallObstacleShapes.Length;
        return LevelGenerationTemplates.WallObstacleShapes[index];
    }

    private List<Vector2Int> GetTemplateWallObstacleOrigins(Vector2Int size)
    {
        List<Vector2Int> origins = new List<Vector2Int>();

        for (int i = 0; i < currentStructureTemplate.obstacleAnchors.Length; i++)
        {
            Vector2Int anchor = currentStructureTemplate.obstacleAnchors[i];

            for (int yOffset = -1; yOffset <= 1; yOffset++)
            {
                for (int xOffset = -1; xOffset <= 1; xOffset++)
                {
                    Vector2Int origin = new Vector2Int(
                        anchor.x - size.x / 2 + xOffset,
                        anchor.y - size.y / 2 + yOffset
                    );

                    if (!ContainsPosition(origins, origin))
                    {
                        origins.Add(origin);
                    }
                }
            }
        }

        return origins;
    }

    private List<Vector2Int> GetWallObstacleOriginCandidates(char[,] grid, string[] shape, Vector2Int size)
    {
        List<Vector2Int> origins = new List<Vector2Int>();
        int minX = 2;
        int minY = 2;
        int maxX = rules.width - size.x - 2;
        int maxY = rules.height - size.y - 2;

        if (maxX < minX || maxY < minY)
        {
            return origins;
        }

        for (int y = minY; y <= maxY; y++)
        {
            for (int x = minX; x <= maxX; x++)
            {
                Vector2Int origin = new Vector2Int(x, y);

                if (CanPlaceWallObstacleShape(grid, origin, shape))
                {
                    origins.Add(origin);
                }
            }
        }

        return origins;
    }

    private bool TryCommitWallObstacleShape(char[,] grid, Vector2Int origin, string[] shape)
    {
        if (!CanPlaceWallObstacleShape(grid, origin, shape))
        {
            return false;
        }

        SetWallObstacleShape(grid, origin, shape, Wall);

        if (HasEnoughGroundCells(grid)
            && AreGroundCellsConnected(grid)
            && ValidateWallTileRules(grid))
        {
            return true;
        }

        SetWallObstacleShape(grid, origin, shape, Ground);
        return false;
    }

    private int GetWallObstacleOriginScore(Vector2Int origin, Vector2Int size)
    {
        Vector2Int center = new Vector2Int(origin.x + size.x / 2, origin.y + size.y / 2);
        Vector2Int mapCenter = GetMapCenter();
        int templateScore = hasDesignBlueprint
            ? GetAnchorScore(center, currentStructureTemplate.obstacleAnchors, 80)
            : 0;
        int routeScore = GetWallObstacleRouteScore(center);

        if (currentObstacleStyle == "side_choke")
        {
            int sideDistance = Mathf.Min(center.x - 1, rules.width - 2 - center.x);
            int verticalCenterDistance = Mathf.Abs(center.y - mapCenter.y);
            return 120 - sideDistance * 10 - verticalCenterDistance * 3 + templateScore + routeScore;
        }

        if (currentObstacleStyle == "goal_guard")
        {
            int edgeDistance = GetDistanceToNearestPlayableEdge(center);
            int horizontalCenterDistance = Mathf.Abs(center.x - mapCenter.x);
            return 120 - edgeDistance * 8 - horizontalCenterDistance * 2 + templateScore + routeScore;
        }

        return 120 - ManhattanDistance(center, mapCenter) * 8 + templateScore + routeScore;
    }

    private int GetWallObstacleRouteScore(Vector2Int center)
    {
        IList<Vector2Int> routeTargets = targets;

        if (routeTargets.Count == 0
            && hasDesignBlueprint
            && currentStructureTemplate.targetAnchors != null)
        {
            routeTargets = currentStructureTemplate.targetAnchors;
        }

        if (routeTargets.Count == 0)
        {
            return 0;
        }

        int nearestTargetDistance = GetNearestDistance(center, routeTargets);
        int score = Mathf.Max(0, 72 - nearestTargetDistance * 16);

        if (IsNearTargetCorridor(center, routeTargets))
        {
            score += 45;
        }

        Vector2Int mapCenter = GetMapCenter();
        score += Mathf.Max(0, 28 - ManhattanDistance(center, mapCenter) * 4);
        return score;
    }

    private bool IsNearTargetCorridor(Vector2Int position, IList<Vector2Int> routeTargets)
    {
        if (routeTargets.Count < 2)
        {
            return false;
        }

        for (int i = 0; i < routeTargets.Count; i++)
        {
            for (int j = i + 1; j < routeTargets.Count; j++)
            {
                if (IsNearAxisAlignedCorridor(position, routeTargets[i], routeTargets[j], 2))
                {
                    return true;
                }
            }
        }

        return false;
    }

    private bool CanPlaceWallObstacleShape(char[,] grid, Vector2Int origin, string[] shape)
    {
        Vector2Int size = GetWallObstacleShapeSize(shape);
        for (int y = origin.y - 1; y <= origin.y + size.y; y++)
        {
            for (int x = origin.x - 1; x <= origin.x + size.x; x++)
            {
                Vector2Int position = new Vector2Int(x, y);

                if (!rules.IsInsidePlayableArea(position))
                {
                    return false;
                }

                if (grid[x, y] != Ground)
                {
                    return false;
                }
            }
        }

        for (int y = origin.y; y < origin.y + size.y; y++)
        {
            for (int x = origin.x; x < origin.x + size.x; x++)
            {
                if (GetWallObstacleShapeTile(shape, x - origin.x, y - origin.y) != Wall)
                {
                    continue;
                }

                Vector2Int position = new Vector2Int(x, y);

                if (grid[x, y] != Ground
                    || targetLookup.Contains(position)
                    || HasWaterBelow(grid, position))
                {
                    return false;
                }
            }
        }

        return true;
    }

    private void SetWallObstacleShape(char[,] grid, Vector2Int origin, string[] shape, char tile)
    {
        Vector2Int size = GetWallObstacleShapeSize(shape);

        for (int y = origin.y; y < origin.y + size.y; y++)
        {
            for (int x = origin.x; x < origin.x + size.x; x++)
            {
                if (GetWallObstacleShapeTile(shape, x - origin.x, y - origin.y) != Wall)
                {
                    continue;
                }

                grid[x, y] = tile;
            }
        }
    }

    private Vector2Int GetWallObstacleShapeSize(string[] shape)
    {
        int width = 0;

        for (int i = 0; i < shape.Length; i++)
        {
            width = Mathf.Max(width, shape[i].Length);
        }

        return new Vector2Int(width, shape.Length);
    }

    private char GetWallObstacleShapeTile(string[] shape, int x, int y)
    {
        if (y < 0 || y >= shape.Length || x < 0 || x >= shape[y].Length)
        {
            return Ground;
        }

        return shape[y][x];
    }

    private bool HasEnoughGroundCells(char[,] grid)
    {
        int requiredCells = rules.boxCount * 2 + 1;
        return CountGroundCells(grid) >= requiredCells;
    }

    private int CountGroundCells(char[,] grid)
    {
        int count = 0;

        for (int y = 1; y < rules.height - 1; y++)
        {
            for (int x = 1; x < rules.width - 1; x++)
            {
                if (grid[x, y] == Ground)
                {
                    count++;
                }
            }
        }

        return count;
    }

    private bool AreGroundCellsConnected(char[,] grid)
    {
        Vector2Int start = Vector2Int.zero;
        bool hasStart = false;
        int groundCellCount = 0;

        for (int y = 1; y < rules.height - 1; y++)
        {
            for (int x = 1; x < rules.width - 1; x++)
            {
                if (grid[x, y] != Ground)
                {
                    continue;
                }

                groundCellCount++;

                if (!hasStart)
                {
                    start = new Vector2Int(x, y);
                    hasStart = true;
                }
            }
        }

        if (!hasStart)
        {
            return false;
        }

        Queue<Vector2Int> open = new Queue<Vector2Int>();
        HashSet<Vector2Int> visited = new HashSet<Vector2Int>();

        open.Enqueue(start);
        visited.Add(start);

        while (open.Count > 0)
        {
            Vector2Int current = open.Dequeue();

            for (int i = 0; i < directions.Length; i++)
            {
                Vector2Int next = current + directions[i];

                if (!rules.IsInsidePlayableArea(next)
                    || visited.Contains(next)
                    || grid[next.x, next.y] != Ground)
                {
                    continue;
                }

                visited.Add(next);
                open.Enqueue(next);
            }
        }

        return visited.Count == groundCellCount;
    }

    private bool TryPlaceTargets(char[,] grid)
    {
        boxes.Clear();
        targets.Clear();
        targetLookup.Clear();

        List<Vector2Int> candidates = GetGroundCells(grid);

        if (candidates.Count < rules.boxCount)
        {
            return false;
        }

        if (TryPlaceTargetsByLayout(candidates))
        {
            return true;
        }

        if (hasDesignBlueprint && TryPlaceTargetsNearTemplateAnchors(candidates))
        {
            return true;
        }

        boxes.Clear();
        targets.Clear();
        targetLookup.Clear();
        Shuffle(candidates);

        for (int i = 0; i < rules.boxCount; i++)
        {
            Vector2Int position = candidates[i];
            targets.Add(position);
            targetLookup.Add(position);
            boxes.Add(position);
        }

        return true;
    }

    private bool TryPlaceTargetsByLayout(List<Vector2Int> candidates)
    {
        if (currentTargetLayout == "clustered")
        {
            return TryPlaceClusteredTargets(candidates, false);
        }

        if (currentTargetLayout == "edge_cluster")
        {
            return TryPlaceClusteredTargets(candidates, true);
        }

        if (currentTargetLayout == "split_pair")
        {
            return TryPlaceSplitTargets(candidates);
        }

        return false;
    }

    private bool TryPlaceTargetsNearTemplateAnchors(List<Vector2Int> candidates)
    {
        if (currentStructureTemplate.targetAnchors.Length < rules.boxCount)
        {
            return false;
        }

        List<int> anchorOrder = new List<int>();

        for (int i = 0; i < currentStructureTemplate.targetAnchors.Length; i++)
        {
            anchorOrder.Add(i);
        }

        Shuffle(anchorOrder);

        for (int startIndex = 0; startIndex < anchorOrder.Count; startIndex++)
        {
            List<Vector2Int> selected = new List<Vector2Int>();
            bool foundAllAnchors = true;

            for (int offset = 0; offset < rules.boxCount; offset++)
            {
                int anchorIndex = anchorOrder[(startIndex + offset) % anchorOrder.Count];
                Vector2Int anchor = currentStructureTemplate.targetAnchors[anchorIndex];
                int bestDistance = int.MaxValue;
                Vector2Int bestCandidate = Vector2Int.zero;
                bool foundCandidate = false;

                for (int i = 0; i < candidates.Count; i++)
                {
                    Vector2Int candidate = candidates[i];

                    if (ContainsPosition(selected, candidate))
                    {
                        continue;
                    }

                    int distance = ManhattanDistance(candidate, anchor);

                    if (distance >= bestDistance)
                    {
                        continue;
                    }

                    bestDistance = distance;
                    bestCandidate = candidate;
                    foundCandidate = true;
                }

                if (!foundCandidate || bestDistance > 4)
                {
                    foundAllAnchors = false;
                    break;
                }

                selected.Add(bestCandidate);
            }

            if (foundAllAnchors && CommitTargetPositions(selected))
            {
                return true;
            }
        }

        return false;
    }

    private bool TryPlaceClusteredTargets(List<Vector2Int> candidates, bool requireEdge)
    {
        List<Vector2Int> seeds = new List<Vector2Int>(candidates);
        Shuffle(seeds);

        for (int i = 0; i < seeds.Count; i++)
        {
            Vector2Int seed = seeds[i];

            if (requireEdge && !IsNearPlayableEdge(seed))
            {
                continue;
            }

            List<Vector2Int> cluster = new List<Vector2Int>();

            for (int j = 0; j < candidates.Count; j++)
            {
                Vector2Int candidate = candidates[j];

                if (requireEdge && !IsNearPlayableEdge(candidate))
                {
                    continue;
                }

                if (ManhattanDistance(seed, candidate) <= 2)
                {
                    cluster.Add(candidate);
                }
            }

            if (cluster.Count < rules.boxCount)
            {
                continue;
            }

            Shuffle(cluster);
            cluster.Sort((left, right) => ManhattanDistance(left, seed).CompareTo(ManhattanDistance(right, seed)));

            if (CommitTargetPositions(cluster))
            {
                return true;
            }
        }

        return false;
    }

    private bool TryPlaceSplitTargets(List<Vector2Int> candidates)
    {
        if (rules.boxCount != 2)
        {
            return false;
        }

        int bestDistance = -1;
        Vector2Int first = Vector2Int.zero;
        Vector2Int second = Vector2Int.zero;

        for (int i = 0; i < candidates.Count; i++)
        {
            for (int j = i + 1; j < candidates.Count; j++)
            {
                int distance = ManhattanDistance(candidates[i], candidates[j]);

                if (distance <= bestDistance)
                {
                    continue;
                }

                bestDistance = distance;
                first = candidates[i];
                second = candidates[j];
            }
        }

        if (bestDistance < Mathf.Max(4, rules.width / 3))
        {
            return false;
        }

        List<Vector2Int> selected = new List<Vector2Int>
        {
            first,
            second
        };

        Shuffle(selected);
        return CommitTargetPositions(selected);
    }

    private bool CommitTargetPositions(List<Vector2Int> selected)
    {
        if (selected.Count < rules.boxCount)
        {
            return false;
        }

        boxes.Clear();
        targets.Clear();
        targetLookup.Clear();

        for (int i = 0; i < rules.boxCount; i++)
        {
            Vector2Int position = selected[i];

            if (targetLookup.Contains(position))
            {
                return false;
            }

            targets.Add(position);
            targetLookup.Add(position);
            boxes.Add(position);
        }

        return true;
    }

    private bool TryPlacePlayer(char[,] grid)
    {
        List<Vector2Int> candidates = GetGroundCells(grid);
        Shuffle(candidates);
        candidates.Sort((left, right) => GetPlayerStartScore(right).CompareTo(GetPlayerStartScore(left)));

        for (int i = 0; i < candidates.Count; i++)
        {
            Vector2Int position = candidates[i];

            if (ContainsPosition(boxes, position) || targetLookup.Contains(position))
            {
                continue;
            }

            playerPosition = position;
            return true;
        }

        return false;
    }

    private int GetPlayerStartScore(Vector2Int position)
    {
        int score = 0;

        for (int i = 0; i < targets.Count; i++)
        {
            score += ManhattanDistance(position, targets[i]) * 4;
        }

        if (hasDesignBlueprint)
        {
            score += GetDistanceToNearestAnchor(position, currentStructureTemplate.targetAnchors) * 2;
        }

        return score;
    }

    private bool TryReversePulls(char[,] grid, int targetPulls, out int reversePulls)
    {
        reversePulls = 0;
        bool[] movedBoxes = new bool[boxes.Count];
        List<ReversePullAction> legalPulls = new List<ReversePullAction>();

        for (int attempt = 0; attempt < rules.maxReverseStepAttempts && reversePulls < targetPulls; attempt++)
        {
            legalPulls.Clear();

            for (int boxIndex = 0; boxIndex < boxes.Count; boxIndex++)
            {
                for (int directionIndex = 0; directionIndex < directions.Length; directionIndex++)
                {
                    Vector2Int direction = directions[directionIndex];

                    if (CanReversePull(grid, boxIndex, direction))
                    {
                        legalPulls.Add(new ReversePullAction(boxIndex, direction));
                    }
                }
            }

            if (legalPulls.Count == 0)
            {
                break;
            }

            ReversePullAction selectedPull = legalPulls[random.Next(legalPulls.Count)];

            if (!TryReversePull(grid, selectedPull.boxIndex, selectedPull.direction))
            {
                continue;
            }

            movedBoxes[selectedPull.boxIndex] = true;
            reversePulls++;
        }

        if (reversePulls < targetPulls)
        {
            return false;
        }

        for (int i = 0; i < movedBoxes.Length; i++)
        {
            if (!movedBoxes[i] || targetLookup.Contains(boxes[i]))
            {
                return false;
            }
        }

        return !targetLookup.Contains(playerPosition);
    }

    private bool HasAdjacentBoxTargetPair()
    {
        for (int boxIndex = 0; boxIndex < boxes.Count; boxIndex++)
        {
            for (int targetIndex = 0; targetIndex < targets.Count; targetIndex++)
            {
                if (ManhattanDistance(boxes[boxIndex], targets[targetIndex]) == 1)
                {
                    return true;
                }
            }
        }

        return false;
    }

    private bool TryReversePull(char[,] grid, int boxIndex, Vector2Int direction)
    {
        if (!CanReversePull(grid, boxIndex, direction))
        {
            return false;
        }

        Vector2Int boxPosition = boxes[boxIndex];
        boxes[boxIndex] = boxPosition - direction;
        playerPosition = boxPosition - new Vector2Int(direction.x * 2, direction.y * 2);
        return true;
    }

    private bool CanReversePull(char[,] grid, int boxIndex, Vector2Int direction)
    {
        Vector2Int boxPosition = boxes[boxIndex];
        Vector2Int nextBoxPosition = boxPosition - direction;
        Vector2Int nextPlayerPosition = boxPosition - new Vector2Int(direction.x * 2, direction.y * 2);

        if (targetLookup.Contains(nextBoxPosition) || targetLookup.Contains(nextPlayerPosition))
        {
            return false;
        }

        if (!CanPlayerOccupy(grid, nextBoxPosition) || !CanPlayerOccupy(grid, nextPlayerPosition))
        {
            return false;
        }

        if (ContainsPositionExcept(boxes, nextBoxPosition, boxIndex)
            || ContainsPositionExcept(boxes, nextPlayerPosition, boxIndex))
        {
            return false;
        }

        if (!CanReach(grid, playerPosition, nextBoxPosition))
        {
            return false;
        }

        return true;
    }

    private bool CanReach(char[,] grid, Vector2Int start, Vector2Int destination)
    {
        if (start == destination)
        {
            return true;
        }

        Queue<Vector2Int> open = new Queue<Vector2Int>();
        HashSet<Vector2Int> visited = new HashSet<Vector2Int>();

        open.Enqueue(start);
        visited.Add(start);

        while (open.Count > 0)
        {
            Vector2Int current = open.Dequeue();

            for (int i = 0; i < directions.Length; i++)
            {
                Vector2Int next = current + directions[i];

                if (visited.Contains(next) || !CanPlayerOccupy(grid, next) || ContainsPosition(boxes, next))
                {
                    continue;
                }

                if (next == destination)
                {
                    return true;
                }

                visited.Add(next);
                open.Enqueue(next);
            }
        }

        return false;
    }

    private bool TryBuildRows(char[,] grid, out string[] rows)
    {
        rows = new string[rules.height];

        if (ContainsPosition(boxes, playerPosition) || targetLookup.Contains(playerPosition))
        {
            return false;
        }

        for (int i = 0; i < boxes.Count; i++)
        {
            if (targetLookup.Contains(boxes[i]))
            {
                return false;
            }
        }

        for (int y = 0; y < rules.height; y++)
        {
            char[] row = new char[rules.width];

            for (int x = 0; x < rules.width; x++)
            {
                row[x] = grid[x, y];
            }

            rows[y] = new string(row);
        }

        for (int i = 0; i < targets.Count; i++)
        {
            SetRowTile(rows, targets[i], Target);
        }

        for (int i = 0; i < boxes.Count; i++)
        {
            SetRowTile(rows, boxes[i], Box);
        }

        SetRowTile(rows, playerPosition, Player);
        return true;
    }

    private void SetRowTile(string[] rows, Vector2Int position, char tile)
    {
        char[] row = rows[position.y].ToCharArray();
        row[position.x] = tile;
        rows[position.y] = new string(row);
    }

    private string[] CloneRows(string[] rows)
    {
        return LevelMapUtilities.CloneRows(rows);
    }
}
