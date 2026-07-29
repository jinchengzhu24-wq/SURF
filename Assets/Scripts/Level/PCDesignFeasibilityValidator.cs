using System.Collections.Generic;
using System.Text;
using UnityEngine;

public static class PCDesignFeasibilityValidator
{
    private const int DefaultMaximumSearchStates = 120000;

    private static readonly Vector2Int[] directions =
    {
        Vector2Int.up,
        Vector2Int.down,
        Vector2Int.left,
        Vector2Int.right
    };

    public static bool TryValidate(string[] sketchRows, out string message)
    {
        if (!TryValidateStartClearance(sketchRows, out message))
        {
            return false;
        }

        return TryValidateOpenCompletion(
            sketchRows,
            DefaultMaximumSearchStates,
            out message
        );
    }

    public static bool TryValidateFeatureCapacity(
        string[] sketchRows,
        int requiredInternalWallTiles,
        int minimumRemainingActivityArea,
        out string message)
    {
        if (!TryGetDimensions(sketchRows, out int width, out int height))
        {
            message = "The PC design has invalid dimensions.";
            return false;
        }

        bool[,] enclosedArea = FindEnclosedArea(
            sketchRows,
            width,
            height
        );
        HashSet<Vector2Int> enclosedCells =
            new HashSet<Vector2Int>();
        List<Vector2Int> wallCandidates =
            new List<Vector2Int>();

        for (int y = 0; y < height; y++)
        {
            for (int x = 0; x < width; x++)
            {
                if (!enclosedArea[x, y])
                {
                    continue;
                }

                Vector2Int position = new Vector2Int(x, y);
                enclosedCells.Add(position);

                if (sketchRows[y][x] == LevelData.Empty
                    && !IsNextToBoxStart(
                        position,
                        sketchRows,
                        width,
                        height))
                {
                    wallCandidates.Add(position);
                }
            }
        }

        for (int y = 0; y < height - 1; y++)
        {
            for (int x = 0; x < width - 1; x++)
            {
                HashSet<Vector2Int> waterCells =
                    new HashSet<Vector2Int>
                    {
                        new Vector2Int(x, y),
                        new Vector2Int(x + 1, y),
                        new Vector2Int(x, y + 1),
                        new Vector2Int(x + 1, y + 1)
                    };

                if (!CanUseAsWaterArea(
                        waterCells,
                        sketchRows,
                        enclosedArea))
                {
                    continue;
                }

                HashSet<Vector2Int> remainingWalkable =
                    new HashSet<Vector2Int>(enclosedCells);
                remainingWalkable.ExceptWith(waterCells);
                List<Vector2Int> availableWalls =
                    new List<Vector2Int>();

                for (int candidate = 0;
                    candidate < wallCandidates.Count;
                    candidate++)
                {
                    if (!waterCells.Contains(wallCandidates[candidate]))
                    {
                        availableWalls.Add(wallCandidates[candidate]);
                    }
                }

                if (remainingWalkable.Count - requiredInternalWallTiles
                        < minimumRemainingActivityArea
                    || availableWalls.Count < requiredInternalWallTiles)
                {
                    continue;
                }

                if (TryChooseConnectedWalls(
                        availableWalls,
                        0,
                        requiredInternalWallTiles,
                        minimumRemainingActivityArea,
                        remainingWalkable))
                {
                    message = "The PC design has room for required generated features.";
                    return true;
                }
            }
        }

        message = "Leave room for one 2x2 water area and "
            + requiredInternalWallTiles
            + " internal wall tiles while retaining "
            + minimumRemainingActivityArea
            + " connected activity cells.";
        return false;
    }

    public static bool TryValidateStartClearance(
        string[] rows,
        out string message)
    {
        if (!TryGetDimensions(rows, out int width, out int height))
        {
            message = "The PC design has invalid dimensions.";
            return false;
        }

        for (int y = 0; y < height; y++)
        {
            for (int x = 0; x < width; x++)
            {
                if (rows[y][x] != LevelData.Box)
                {
                    continue;
                }

                Vector2Int position = new Vector2Int(x, y);

                for (int direction = 0; direction < directions.Length; direction++)
                {
                    Vector2Int neighbor = position + directions[direction];

                    if (IsInside(neighbor, width, height)
                        && rows[neighbor.y][neighbor.x] == LevelData.Wall)
                    {
                        message = "Box start at row "
                            + (y + 1)
                            + ", column "
                            + (x + 1)
                            + " cannot touch a wall.";
                        return false;
                    }
                }
            }
        }

        message = "";
        return true;
    }

    public static bool TryValidateOpenCompletion(
        string[] sketchRows,
        int maximumSearchStates,
        out string message)
    {
        if (!TryGetDimensions(sketchRows, out int width, out int height))
        {
            message = "The PC design has invalid dimensions.";
            return false;
        }

        bool[,] enclosedArea = FindEnclosedArea(sketchRows, width, height);
        HashSet<Vector2Int> walkable = new HashSet<Vector2Int>();
        List<Vector2Int> startBoxes = new List<Vector2Int>();
        HashSet<Vector2Int> targets = new HashSet<Vector2Int>();

        for (int y = 0; y < height; y++)
        {
            for (int x = 0; x < width; x++)
            {
                if (!enclosedArea[x, y])
                {
                    continue;
                }

                Vector2Int position = new Vector2Int(x, y);
                walkable.Add(position);

                if (sketchRows[y][x] == LevelData.Box)
                {
                    startBoxes.Add(position);
                }
                else if (sketchRows[y][x] == LevelData.Target)
                {
                    targets.Add(position);
                }
            }
        }

        if (startBoxes.Count == 0 || startBoxes.Count != targets.Count)
        {
            message = "The PC design needs matching box starts and targets.";
            return false;
        }

        SortPositions(startBoxes);
        HashSet<Vector2Int> initialBoxSet =
            new HashSet<Vector2Int>(startBoxes);
        List<Vector2Int> initialPlayerRegions = FindRegionRepresentatives(
            walkable,
            initialBoxSet
        );

        if (initialPlayerRegions.Count == 0)
        {
            message = "The PC design has no possible player start.";
            return false;
        }

        Queue<SolverState> openStates = new Queue<SolverState>();
        HashSet<string> visitedStates = new HashSet<string>();

        for (int region = 0; region < initialPlayerRegions.Count; region++)
        {
            Vector2Int player = initialPlayerRegions[region];
            string key = BuildStateKey(
                player,
                startBoxes,
                walkable
            );

            if (visitedStates.Add(key))
            {
                openStates.Enqueue(
                    new SolverState(player, new List<Vector2Int>(startBoxes))
                );
            }
        }

        int searchedStates = 0;
        int searchLimit = Mathf.Max(1, maximumSearchStates);

        while (openStates.Count > 0 && searchedStates < searchLimit)
        {
            SolverState state = openStates.Dequeue();
            searchedStates++;

            if (AreBoxesOnTargets(state.boxes, targets))
            {
                message = "The open version of this PC design is solvable.";
                return true;
            }

            HashSet<Vector2Int> boxSet =
                new HashSet<Vector2Int>(state.boxes);
            HashSet<Vector2Int> reachable = FindReachableCells(
                state.player,
                walkable,
                boxSet
            );

            for (int boxIndex = 0;
                boxIndex < state.boxes.Count;
                boxIndex++)
            {
                Vector2Int box = state.boxes[boxIndex];

                for (int direction = 0;
                    direction < directions.Length;
                    direction++)
                {
                    Vector2Int pushDirection = directions[direction];
                    Vector2Int standingCell = box - pushDirection;
                    Vector2Int destination = box + pushDirection;

                    if (!reachable.Contains(standingCell)
                        || !walkable.Contains(destination)
                        || boxSet.Contains(destination))
                    {
                        continue;
                    }

                    List<Vector2Int> nextBoxes =
                        new List<Vector2Int>(state.boxes);
                    nextBoxes[boxIndex] = destination;
                    SortPositions(nextBoxes);
                    Vector2Int nextPlayer = box;
                    string key = BuildStateKey(
                        nextPlayer,
                        nextBoxes,
                        walkable
                    );

                    if (visitedStates.Add(key))
                    {
                        openStates.Enqueue(
                            new SolverState(nextPlayer, nextBoxes)
                        );
                    }
                }
            }
        }

        message = searchedStates >= searchLimit
            ? "The open version of this PC design exceeded the local solvability check."
            : "The open version of this PC design is not solvable. Move box starts, targets, or walls.";
        return false;
    }

    private static bool TryGetDimensions(
        string[] rows,
        out int width,
        out int height)
    {
        width = 0;
        height = rows != null ? rows.Length : 0;

        if (rows == null || rows.Length == 0 || rows[0] == null)
        {
            return false;
        }

        width = rows[0].Length;

        if (width == 0)
        {
            return false;
        }

        for (int row = 0; row < rows.Length; row++)
        {
            if (rows[row] == null || rows[row].Length != width)
            {
                return false;
            }
        }

        return true;
    }

    private static bool[,] FindEnclosedArea(
        string[] rows,
        int width,
        int height)
    {
        bool[,] outside = new bool[width, height];
        Queue<Vector2Int> open = new Queue<Vector2Int>();

        for (int x = 0; x < width; x++)
        {
            EnqueueOutside(
                new Vector2Int(x, 0),
                rows,
                width,
                height,
                outside,
                open
            );
            EnqueueOutside(
                new Vector2Int(x, height - 1),
                rows,
                width,
                height,
                outside,
                open
            );
        }

        for (int y = 0; y < height; y++)
        {
            EnqueueOutside(
                new Vector2Int(0, y),
                rows,
                width,
                height,
                outside,
                open
            );
            EnqueueOutside(
                new Vector2Int(width - 1, y),
                rows,
                width,
                height,
                outside,
                open
            );
        }

        while (open.Count > 0)
        {
            Vector2Int current = open.Dequeue();

            for (int direction = 0; direction < directions.Length; direction++)
            {
                EnqueueOutside(
                    current + directions[direction],
                    rows,
                    width,
                    height,
                    outside,
                    open
                );
            }
        }

        bool[,] enclosed = new bool[width, height];

        for (int y = 0; y < height; y++)
        {
            for (int x = 0; x < width; x++)
            {
                enclosed[x, y] = rows[y][x] != LevelData.Wall
                    && !outside[x, y];
            }
        }

        return enclosed;
    }

    private static bool CanUseAsWaterArea(
        HashSet<Vector2Int> waterCells,
        string[] sketchRows,
        bool[,] enclosedArea)
    {
        foreach (Vector2Int position in waterCells)
        {
            if (!enclosedArea[position.x, position.y]
                || sketchRows[position.y][position.x] != LevelData.Empty)
            {
                return false;
            }
        }

        return true;
    }

    private static bool IsNextToBoxStart(
        Vector2Int position,
        string[] sketchRows,
        int width,
        int height)
    {
        for (int direction = 0; direction < directions.Length; direction++)
        {
            Vector2Int neighbor = position + directions[direction];

            if (IsInside(neighbor, width, height)
                && sketchRows[neighbor.y][neighbor.x] == LevelData.Box)
            {
                return true;
            }
        }

        return false;
    }

    private static bool TryChooseConnectedWalls(
        List<Vector2Int> wallCandidates,
        int startIndex,
        int wallsRemaining,
        int minimumRemainingActivityArea,
        HashSet<Vector2Int> remainingWalkable)
    {
        if (wallsRemaining == 0)
        {
            return remainingWalkable.Count >= minimumRemainingActivityArea
                && IsSingleConnectedArea(remainingWalkable);
        }

        int finalStart = wallCandidates.Count - wallsRemaining;

        for (int index = startIndex; index <= finalStart; index++)
        {
            Vector2Int wall = wallCandidates[index];

            if (!remainingWalkable.Remove(wall))
            {
                continue;
            }

            if (TryChooseConnectedWalls(
                    wallCandidates,
                    index + 1,
                    wallsRemaining - 1,
                    minimumRemainingActivityArea,
                    remainingWalkable))
            {
                return true;
            }

            remainingWalkable.Add(wall);
        }

        return false;
    }

    private static bool IsSingleConnectedArea(
        HashSet<Vector2Int> cells)
    {
        if (cells.Count == 0)
        {
            return false;
        }

        Vector2Int start = GetFirstPosition(cells);
        HashSet<Vector2Int> visited =
            new HashSet<Vector2Int> { start };
        Queue<Vector2Int> open = new Queue<Vector2Int>();
        open.Enqueue(start);

        while (open.Count > 0)
        {
            Vector2Int current = open.Dequeue();

            for (int direction = 0; direction < directions.Length; direction++)
            {
                Vector2Int neighbor = current + directions[direction];

                if (cells.Contains(neighbor) && visited.Add(neighbor))
                {
                    open.Enqueue(neighbor);
                }
            }
        }

        return visited.Count == cells.Count;
    }

    private static void EnqueueOutside(
        Vector2Int position,
        string[] rows,
        int width,
        int height,
        bool[,] visited,
        Queue<Vector2Int> open)
    {
        if (!IsInside(position, width, height)
            || visited[position.x, position.y]
            || rows[position.y][position.x] == LevelData.Wall)
        {
            return;
        }

        visited[position.x, position.y] = true;
        open.Enqueue(position);
    }

    private static List<Vector2Int> FindRegionRepresentatives(
        HashSet<Vector2Int> walkable,
        HashSet<Vector2Int> boxes)
    {
        HashSet<Vector2Int> remaining =
            new HashSet<Vector2Int>(walkable);
        remaining.ExceptWith(boxes);
        List<Vector2Int> representatives = new List<Vector2Int>();
        Queue<Vector2Int> open = new Queue<Vector2Int>();

        while (remaining.Count > 0)
        {
            Vector2Int representative = GetFirstPosition(remaining);
            representatives.Add(representative);
            remaining.Remove(representative);
            open.Enqueue(representative);

            while (open.Count > 0)
            {
                Vector2Int current = open.Dequeue();

                for (int direction = 0;
                    direction < directions.Length;
                    direction++)
                {
                    Vector2Int neighbor = current + directions[direction];

                    if (remaining.Remove(neighbor))
                    {
                        open.Enqueue(neighbor);
                    }
                }
            }
        }

        return representatives;
    }

    private static HashSet<Vector2Int> FindReachableCells(
        Vector2Int player,
        HashSet<Vector2Int> walkable,
        HashSet<Vector2Int> boxes)
    {
        HashSet<Vector2Int> reachable = new HashSet<Vector2Int>();

        if (!walkable.Contains(player) || boxes.Contains(player))
        {
            return reachable;
        }

        Queue<Vector2Int> open = new Queue<Vector2Int>();
        reachable.Add(player);
        open.Enqueue(player);

        while (open.Count > 0)
        {
            Vector2Int current = open.Dequeue();

            for (int direction = 0; direction < directions.Length; direction++)
            {
                Vector2Int neighbor = current + directions[direction];

                if (walkable.Contains(neighbor)
                    && !boxes.Contains(neighbor)
                    && reachable.Add(neighbor))
                {
                    open.Enqueue(neighbor);
                }
            }
        }

        return reachable;
    }

    private static string BuildStateKey(
        Vector2Int player,
        List<Vector2Int> boxes,
        HashSet<Vector2Int> walkable)
    {
        HashSet<Vector2Int> reachable = FindReachableCells(
            player,
            walkable,
            new HashSet<Vector2Int>(boxes)
        );
        Vector2Int representative = GetFirstPosition(reachable);
        StringBuilder builder = new StringBuilder();
        builder.Append(representative.x);
        builder.Append(',');
        builder.Append(representative.y);
        builder.Append('|');

        for (int box = 0; box < boxes.Count; box++)
        {
            builder.Append(boxes[box].x);
            builder.Append(',');
            builder.Append(boxes[box].y);
            builder.Append(';');
        }

        return builder.ToString();
    }

    private static Vector2Int GetFirstPosition(
        HashSet<Vector2Int> positions)
    {
        Vector2Int first = Vector2Int.zero;
        bool hasFirst = false;

        foreach (Vector2Int position in positions)
        {
            if (!hasFirst
                || position.y < first.y
                || (position.y == first.y && position.x < first.x))
            {
                first = position;
                hasFirst = true;
            }
        }

        return first;
    }

    private static bool AreBoxesOnTargets(
        List<Vector2Int> boxes,
        HashSet<Vector2Int> targets)
    {
        for (int box = 0; box < boxes.Count; box++)
        {
            if (!targets.Contains(boxes[box]))
            {
                return false;
            }
        }

        return true;
    }

    private static void SortPositions(List<Vector2Int> positions)
    {
        positions.Sort(
            (a, b) =>
            {
                int yComparison = a.y.CompareTo(b.y);
                return yComparison != 0
                    ? yComparison
                    : a.x.CompareTo(b.x);
            }
        );
    }

    private static bool IsInside(
        Vector2Int position,
        int width,
        int height)
    {
        return position.x >= 0
            && position.x < width
            && position.y >= 0
            && position.y < height;
    }

    private sealed class SolverState
    {
        public readonly Vector2Int player;
        public readonly List<Vector2Int> boxes;

        public SolverState(
            Vector2Int player,
            List<Vector2Int> boxes)
        {
            this.player = player;
            this.boxes = boxes;
        }
    }
}
