using System;
using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public partial class LevelGenerator
{
    private int CountTiles(string[] rows, char tile)
    {
        int count = 0;

        for (int y = 0; y < rows.Length; y++)
        {
            string row = rows[y] ?? "";

            for (int x = 0; x < row.Length; x++)
            {
                if (row[x] == tile)
                {
                    count++;
                }
            }
        }

        return count;
    }

    private int CountWaterAreas(string[] rows)
    {
        HashSet<Vector2Int> visited = new HashSet<Vector2Int>();
        int waterAreas = 0;

        for (int y = 0; y < rows.Length; y++)
        {
            string row = rows[y] ?? "";

            for (int x = 0; x < row.Length; x++)
            {
                Vector2Int position = new Vector2Int(x, y);

                if (row[x] != Water || visited.Contains(position))
                {
                    continue;
                }

                waterAreas++;
                VisitWaterArea(rows, position, visited);
            }
        }

        return waterAreas;
    }

    private void VisitWaterArea(string[] rows, Vector2Int start, HashSet<Vector2Int> visited)
    {
        Queue<Vector2Int> open = new Queue<Vector2Int>();
        open.Enqueue(start);
        visited.Add(start);

        while (open.Count > 0)
        {
            Vector2Int current = open.Dequeue();

            for (int i = 0; i < directions.Length; i++)
            {
                Vector2Int next = current + directions[i];

                if (visited.Contains(next) || LevelData.GetMapTile(rows, next) != Water)
                {
                    continue;
                }

                visited.Add(next);
                open.Enqueue(next);
            }
        }
    }

    private int CountSurroundedWalls(string[] rows)
    {
        int count = 0;

        for (int y = 0; y < rows.Length; y++)
        {
            string row = rows[y] ?? "";

            for (int x = 0; x < row.Length; x++)
            {
                if (LevelData.IsSurroundedWall(rows, new Vector2Int(x, y)))
                {
                    count++;
                }
            }
        }

        return count;
    }

    private int GetTargetDistance(string[] rows)
    {
        List<Vector2Int> targetPositions = new List<Vector2Int>();

        for (int y = 0; y < rows.Length; y++)
        {
            string row = rows[y] ?? "";

            for (int x = 0; x < row.Length; x++)
            {
                if (row[x] == Target)
                {
                    targetPositions.Add(new Vector2Int(x, y));
                }
            }
        }

        int distance = 0;

        for (int i = 0; i < targetPositions.Count; i++)
        {
            for (int j = i + 1; j < targetPositions.Count; j++)
            {
                distance += ManhattanDistance(targetPositions[i], targetPositions[j]);
            }
        }

        return distance;
    }

    private int CountObstacleInfluence(string[] rows)
    {
        List<Vector2Int> targetPositions = GetRowsPositions(rows, Target);
        List<Vector2Int> boxPositions = GetRowsPositions(rows, Box);
        List<Vector2Int> playerPositions = GetRowsPositions(rows, Player);

        if (targetPositions.Count == 0 || boxPositions.Count == 0)
        {
            return 0;
        }

        int influence = 0;

        for (int y = 1; y < rows.Length - 1; y++)
        {
            string row = rows[y] ?? "";

            for (int x = 1; x < row.Length - 1; x++)
            {
                Vector2Int position = new Vector2Int(x, y);

                if (LevelData.GetMapTile(rows, position) != Wall)
                {
                    continue;
                }

                int tileInfluence = 0;
                int nearestTargetDistance = GetNearestDistance(position, targetPositions);
                int nearestBoxDistance = GetNearestDistance(position, boxPositions);

                if (nearestTargetDistance <= 2)
                {
                    tileInfluence += 2;
                }
                else if (nearestTargetDistance <= 3)
                {
                    tileInfluence++;
                }

                if (nearestBoxDistance <= 2)
                {
                    tileInfluence += 2;
                }
                else if (nearestBoxDistance <= 3)
                {
                    tileInfluence++;
                }

                if (IsNearAnyBoxTargetCorridor(position, boxPositions, targetPositions))
                {
                    tileInfluence += 2;
                }

                if (playerPositions.Count > 0 && GetNearestDistance(position, playerPositions) <= 4)
                {
                    tileInfluence++;
                }

                if (IsChokeInfluenceWall(rows, position))
                {
                    tileInfluence++;
                }

                influence += Mathf.Min(tileInfluence, 5);
            }
        }

        return influence;
    }

    private int GetBoxInteractionScore(string[] rows)
    {
        List<Vector2Int> boxPositions = GetRowsPositions(rows, Box);
        List<Vector2Int> targetPositions = GetRowsPositions(rows, Target);

        if (boxPositions.Count != 2 || targetPositions.Count < 2)
        {
            return 0;
        }

        int score = 0;
        int boxDistance = ManhattanDistance(boxPositions[0], boxPositions[1]);
        int targetDistance = ManhattanDistance(targetPositions[0], targetPositions[1]);

        if (boxDistance <= 2)
        {
            score += 2;
        }
        else if (boxDistance <= 4)
        {
            score += 4;
        }
        else if (boxDistance <= 6)
        {
            score += 3;
        }
        else if (boxDistance <= 8)
        {
            score += 1;
        }

        if (targetDistance <= 3)
        {
            score += 3;
        }
        else if (targetDistance <= 5)
        {
            score += 2;
        }
        else if (targetDistance <= 7)
        {
            score += 1;
        }

        int sharedRouteCells = CountSharedBoxRouteCells(rows, boxPositions, targetPositions);
        int sharedChokeWalls = CountSharedBoxChokeWalls(rows, boxPositions, targetPositions);
        score += Mathf.Min(sharedRouteCells / 3, 3);
        score += Mathf.Min(sharedChokeWalls, 3);

        if (boxDistance >= 9 && targetDistance >= 8)
        {
            score = Mathf.Max(0, score - 2);
        }

        return score;
    }

    private int CountSharedBoxRouteCells(
        string[] rows,
        List<Vector2Int> boxPositions,
        List<Vector2Int> targetPositions)
    {
        int count = 0;

        for (int y = 1; y < rows.Length - 1; y++)
        {
            string row = rows[y] ?? "";

            for (int x = 1; x < row.Length - 1; x++)
            {
                Vector2Int position = new Vector2Int(x, y);

                if (!IsRowsWalkableForInfluence(rows, position))
                {
                    continue;
                }

                if (IsNearBoxTargetRoute(position, boxPositions[0], targetPositions)
                    && IsNearBoxTargetRoute(position, boxPositions[1], targetPositions))
                {
                    count++;
                }
            }
        }

        return count;
    }

    private int CountSharedBoxChokeWalls(
        string[] rows,
        List<Vector2Int> boxPositions,
        List<Vector2Int> targetPositions)
    {
        int count = 0;

        for (int y = 1; y < rows.Length - 1; y++)
        {
            string row = rows[y] ?? "";

            for (int x = 1; x < row.Length - 1; x++)
            {
                Vector2Int position = new Vector2Int(x, y);

                if (LevelData.GetMapTile(rows, position) != Wall || !IsChokeInfluenceWall(rows, position))
                {
                    continue;
                }

                if (IsNearBoxTargetRoute(position, boxPositions[0], targetPositions)
                    && IsNearBoxTargetRoute(position, boxPositions[1], targetPositions))
                {
                    count++;
                }
            }
        }

        return count;
    }

    private bool IsNearBoxTargetRoute(
        Vector2Int position,
        Vector2Int boxPosition,
        List<Vector2Int> targetPositions)
    {
        for (int i = 0; i < targetPositions.Count; i++)
        {
            if (IsNearAxisAlignedCorridor(position, boxPosition, targetPositions[i], 1))
            {
                return true;
            }
        }

        return false;
    }

    private List<Vector2Int> GetRowsPositions(string[] rows, char tile)
    {
        List<Vector2Int> positions = new List<Vector2Int>();

        for (int y = 0; y < rows.Length; y++)
        {
            string row = rows[y] ?? "";

            for (int x = 0; x < row.Length; x++)
            {
                if (row[x] == tile)
                {
                    positions.Add(new Vector2Int(x, y));
                }
            }
        }

        return positions;
    }

    private bool IsNearAnyBoxTargetCorridor(
        Vector2Int position,
        List<Vector2Int> boxPositions,
        List<Vector2Int> targetPositions)
    {
        for (int boxIndex = 0; boxIndex < boxPositions.Count; boxIndex++)
        {
            for (int targetIndex = 0; targetIndex < targetPositions.Count; targetIndex++)
            {
                if (IsNearAxisAlignedCorridor(
                        position,
                        boxPositions[boxIndex],
                        targetPositions[targetIndex],
                        2))
                {
                    return true;
                }
            }
        }

        return false;
    }

    private bool IsChokeInfluenceWall(string[] rows, Vector2Int position)
    {
        bool leftOpen = IsRowsWalkableForInfluence(rows, position + new Vector2Int(-1, 0));
        bool rightOpen = IsRowsWalkableForInfluence(rows, position + new Vector2Int(1, 0));
        bool upOpen = IsRowsWalkableForInfluence(rows, position + new Vector2Int(0, -1));
        bool downOpen = IsRowsWalkableForInfluence(rows, position + new Vector2Int(0, 1));
        return (leftOpen && rightOpen && !upOpen && !downOpen)
            || (upOpen && downOpen && !leftOpen && !rightOpen);
    }

    private bool IsRowsWalkableForInfluence(string[] rows, Vector2Int position)
    {
        return LevelData.IsGround(LevelData.GetMapTile(rows, position));
    }

    private List<Vector2Int> GetGroundCells(char[,] grid)
    {
        List<Vector2Int> cells = new List<Vector2Int>();

        for (int y = 1; y < rules.height - 1; y++)
        {
            for (int x = 1; x < rules.width - 1; x++)
            {
                if (grid[x, y] == Ground)
                {
                    cells.Add(new Vector2Int(x, y));
                }
            }
        }

        return cells;
    }

    private bool IsRectClear(char[,] grid, Vector2Int origin, Vector2Int size)
    {
        for (int y = origin.y; y < origin.y + size.y; y++)
        {
            for (int x = origin.x; x < origin.x + size.x; x++)
            {
                if (grid[x, y] != Ground)
                {
                    return false;
                }
            }
        }

        return true;
    }

    private bool CanPlaceWaterRect(char[,] grid, Vector2Int origin, Vector2Int size)
    {
        if (!IsRectClear(grid, origin, size))
        {
            return false;
        }

        for (int y = origin.y; y < origin.y + size.y; y++)
        {
            for (int x = origin.x; x < origin.x + size.x; x++)
            {
                if (HasWallAbove(grid, new Vector2Int(x, y)))
                {
                    return false;
                }
            }
        }

        return true;
    }

    private bool ValidateTileRules(char[,] grid, bool logFailure = false)
    {
        for (int y = 0; y < rules.height; y++)
        {
            for (int x = 0; x < rules.width; x++)
            {
                Vector2Int position = new Vector2Int(x, y);

                if (grid[x, y] == Water && HasWallAbove(grid, position))
                {
                    if (logFailure && logGenerationResult)
                    {
                        Debug.LogWarning("LevelGenerator: Water tile rejected below wall at " + position);
                    }

                    return false;
                }
            }
        }

        return ValidateWallTileRules(grid, logFailure);
    }

    private bool ValidateWallTileRules(char[,] grid, bool logFailure = false)
    {
        if (!ValidateNoParallelWallRows(grid, logFailure))
        {
            return false;
        }

        for (int y = 0; y < rules.height; y++)
        {
            for (int x = 0; x < rules.width; x++)
            {
                Vector2Int position = new Vector2Int(x, y);

                if (grid[x, y] == Wall && !IsSupportedWallTileShape(grid, position))
                {
                    if (logFailure && logGenerationResult)
                    {
                        Debug.LogWarning(
                            "LevelGenerator: Wall tile rule rejected wall at "
                            + position
                            + " neighbors="
                            + GetTileNeighborhoodDebug(grid, position)
                        );
                    }

                    return false;
                }
            }
        }

        return true;
    }

    private bool ValidateNoParallelWallRows(char[,] grid, bool logFailure = false)
    {
        for (int y = 0; y < rules.height - 1; y++)
        {
            int runStartX = -1;
            int runLength = 0;

            for (int x = 0; x < rules.width; x++)
            {
                bool parallelWall = grid[x, y] == Wall && grid[x, y + 1] == Wall;

                if (parallelWall)
                {
                    if (runLength == 0)
                    {
                        runStartX = x;
                    }

                    runLength++;
                    continue;
                }

                if (runLength >= 2
                    && !IsAllowedOuterShellWallJoin(grid, runStartX, y, runLength))
                {
                    return RejectParallelWallRows(runStartX, y, runLength, logFailure);
                }

                runStartX = -1;
                runLength = 0;
            }

            if (runLength >= 2
                && !IsAllowedOuterShellWallJoin(grid, runStartX, y, runLength))
            {
                return RejectParallelWallRows(runStartX, y, runLength, logFailure);
            }
        }

        return true;
    }

    private bool IsAllowedOuterShellWallJoin(
        char[,] grid,
        int startX,
        int startY,
        int length)
    {
        if (length != 2)
        {
            return false;
        }

        for (int y = startY; y <= startY + 1; y++)
        {
            for (int x = startX; x < startX + length; x++)
            {
                Vector2Int position = new Vector2Int(x, y);

                for (int directionIndex = 0; directionIndex < directions.Length; directionIndex++)
                {
                    Vector2Int neighbor = position + directions[directionIndex];

                    if (!rules.IsInsideMap(neighbor)
                        || grid[neighbor.x, neighbor.y] == Empty)
                    {
                        return true;
                    }
                }
            }
        }

        return false;
    }

    private bool RejectParallelWallRows(int startX, int y, int length, bool logFailure)
    {
        if (logFailure && logGenerationResult)
        {
            Debug.LogWarning(
                "LevelGenerator: Parallel wall rows rejected at "
                + new Vector2Int(startX, y)
                + " length="
                + length
            );
        }

        return false;
    }

    private string GetTileNeighborhoodDebug(char[,] grid, Vector2Int position)
    {
        return "["
            + GetDebugTile(grid, position + new Vector2Int(-1, -1))
            + GetDebugTile(grid, position + new Vector2Int(0, -1))
            + GetDebugTile(grid, position + new Vector2Int(1, -1))
            + "/"
            + GetDebugTile(grid, position + new Vector2Int(-1, 0))
            + GetDebugTile(grid, position)
            + GetDebugTile(grid, position + new Vector2Int(1, 0))
            + "/"
            + GetDebugTile(grid, position + new Vector2Int(-1, 1))
            + GetDebugTile(grid, position + new Vector2Int(0, 1))
            + GetDebugTile(grid, position + new Vector2Int(1, 1))
            + "]";
    }

    private char GetDebugTile(char[,] grid, Vector2Int position)
    {
        if (!rules.IsInsideMap(position))
        {
            return '!';
        }

        return grid[position.x, position.y];
    }

    private bool IsSupportedWallTileShape(char[,] grid, Vector2Int position)
    {
        bool up = IsWallAt(grid, position + new Vector2Int(0, -1));
        bool down = IsWallAt(grid, position + new Vector2Int(0, 1));
        bool left = IsWallAt(grid, position + new Vector2Int(-1, 0));
        bool right = IsWallAt(grid, position + new Vector2Int(1, 0));
        bool rightDown = IsWallAt(grid, position + new Vector2Int(1, 1));

        return LevelData.IsSupportedWallShape(
            IsSurroundedWallTileShape(grid, position),
            up,
            down,
            left,
            right,
            rightDown
        );
    }

    private bool IsSurroundedWallTileShape(char[,] grid, Vector2Int position)
    {
        return IsWallAt(grid, position)
            && HasTilesAround(grid, position)
            && !IsWaterAt(grid, position + new Vector2Int(0, 1));
    }

    private bool HasTilesAround(char[,] grid, Vector2Int position)
    {
        for (int yOffset = -1; yOffset <= 1; yOffset++)
        {
            for (int xOffset = -1; xOffset <= 1; xOffset++)
            {
                if (xOffset == 0 && yOffset == 0)
                {
                    continue;
                }

                if (!HasTileAt(grid, position + new Vector2Int(xOffset, yOffset)))
                {
                    return false;
                }
            }
        }

        return true;
    }

    private bool HasTileAt(char[,] grid, Vector2Int position)
    {
        return rules.IsInsideMap(position) && grid[position.x, position.y] != Empty;
    }

    private bool IsWallAt(char[,] grid, Vector2Int position)
    {
        return rules.IsInsideMap(position) && grid[position.x, position.y] == Wall;
    }

    private bool IsWaterAt(char[,] grid, Vector2Int position)
    {
        return rules.IsInsideMap(position) && grid[position.x, position.y] == Water;
    }

    private bool HasWallAbove(char[,] grid, Vector2Int position)
    {
        Vector2Int above = position + new Vector2Int(0, -1);
        return rules.IsInsideMap(above) && grid[above.x, above.y] == Wall;
    }

    private bool HasWaterBelow(char[,] grid, Vector2Int position)
    {
        Vector2Int below = position + new Vector2Int(0, 1);
        return rules.IsInsideMap(below) && grid[below.x, below.y] == Water;
    }

    private bool CanPlayerOccupy(char[,] grid, Vector2Int position)
    {
        return rules.IsInsidePlayableArea(position)
            && grid[position.x, position.y] == Ground;
    }

    private bool ContainsPosition(List<Vector2Int> positions, Vector2Int position)
    {
        for (int i = 0; i < positions.Count; i++)
        {
            if (positions[i] == position)
            {
                return true;
            }
        }

        return false;
    }

    private bool ContainsPositionExcept(List<Vector2Int> positions, Vector2Int position, int ignoredIndex)
    {
        for (int i = 0; i < positions.Count; i++)
        {
            if (i != ignoredIndex && positions[i] == position)
            {
                return true;
            }
        }

        return false;
    }

    private Vector2Int GetMapCenter()
    {
        return new Vector2Int(rules.width / 2, rules.height / 2);
    }

    private int ManhattanDistance(Vector2Int first, Vector2Int second)
    {
        return Mathf.Abs(first.x - second.x) + Mathf.Abs(first.y - second.y);
    }

    private int GetNearestDistance(Vector2Int position, IList<Vector2Int> positions)
    {
        if (positions == null || positions.Count == 0)
        {
            return int.MaxValue;
        }

        int bestDistance = int.MaxValue;

        for (int i = 0; i < positions.Count; i++)
        {
            bestDistance = Mathf.Min(bestDistance, ManhattanDistance(position, positions[i]));
        }

        return bestDistance;
    }

    private bool IsNearAxisAlignedCorridor(
        Vector2Int position,
        Vector2Int first,
        Vector2Int second,
        int tolerance)
    {
        int minX = Mathf.Min(first.x, second.x) - tolerance;
        int maxX = Mathf.Max(first.x, second.x) + tolerance;
        int minY = Mathf.Min(first.y, second.y) - tolerance;
        int maxY = Mathf.Max(first.y, second.y) + tolerance;

        bool insideHorizontalRange = position.x >= minX
            && position.x <= maxX;
        bool insideVerticalRange = position.y >= minY
            && position.y <= maxY;
        bool nearHorizontalCorridor = insideHorizontalRange
            && (Mathf.Abs(position.y - first.y) <= tolerance
                || Mathf.Abs(position.y - second.y) <= tolerance);
        bool nearVerticalCorridor = insideVerticalRange
            && (Mathf.Abs(position.x - first.x) <= tolerance
                || Mathf.Abs(position.x - second.x) <= tolerance);

        return nearHorizontalCorridor || nearVerticalCorridor;
    }

    private bool IsNearPlayableEdge(Vector2Int position)
    {
        return GetDistanceToNearestPlayableEdge(position) <= 2;
    }

    private int GetAnchorScore(Vector2Int position, Vector2Int[] anchors, int maxScore)
    {
        if (!hasDesignBlueprint || anchors == null || anchors.Length == 0)
        {
            return 0;
        }

        return Mathf.Max(0, maxScore - GetDistanceToNearestAnchor(position, anchors) * 12);
    }

    private int GetDistanceToNearestAnchor(Vector2Int position, Vector2Int[] anchors)
    {
        if (anchors == null || anchors.Length == 0)
        {
            return 0;
        }

        int bestDistance = int.MaxValue;

        for (int i = 0; i < anchors.Length; i++)
        {
            bestDistance = Mathf.Min(bestDistance, ManhattanDistance(position, anchors[i]));
        }

        return bestDistance;
    }

    private int GetDistanceToNearestPlayableEdge(Vector2Int position)
    {
        int left = position.x - 1;
        int right = rules.width - 2 - position.x;
        int top = position.y - 1;
        int bottom = rules.height - 2 - position.y;
        return Mathf.Min(Mathf.Min(left, right), Mathf.Min(top, bottom));
    }

    private int GetDistanceToNearestPlayableCorner(Vector2Int position)
    {
        int topLeft = ManhattanDistance(position, new Vector2Int(1, 1));
        int topRight = ManhattanDistance(position, new Vector2Int(rules.width - 2, 1));
        int bottomLeft = ManhattanDistance(position, new Vector2Int(1, rules.height - 2));
        int bottomRight = ManhattanDistance(position, new Vector2Int(rules.width - 2, rules.height - 2));
        return Mathf.Min(Mathf.Min(topLeft, topRight), Mathf.Min(bottomLeft, bottomRight));
    }

    private void Shuffle<T>(List<T> values)
    {
        for (int i = values.Count - 1; i > 0; i--)
        {
            int swapIndex = random.Next(i + 1);
            T value = values[i];
            values[i] = values[swapIndex];
            values[swapIndex] = value;
        }
    }

    private int NextInclusive(int min, int max)
    {
        return random.Next(min, max + 1);
    }

    private int GetRuntimeSeed()
    {
        unchecked
        {
            runtimeSeedCounter++;
            return System.Environment.TickCount
                ^ (runtimeSeedCounter * 397)
                ^ (GetInstanceID() * 17);
        }
    }
}
