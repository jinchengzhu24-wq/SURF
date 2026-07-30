using System.Collections.Generic;
using UnityEngine;

public static class PCLevelCandidateValidator
{
    private static readonly Vector2Int[] directions =
    {
        Vector2Int.up,
        Vector2Int.down,
        Vector2Int.left,
        Vector2Int.right
    };

    public static bool TryValidate(
        PCDesignSketchData sketch,
        string[] candidateRows,
        int minimumActivityArea,
        int minimumInternalWallTiles,
        int minimumWaterAreas,
        int minimumWaterWidth,
        int minimumWaterHeight,
        int maximumWaterWidth,
        int maximumWaterHeight,
        string competitionMode,
        out string message)
    {
        if (!TryValidateDimensions(sketch, candidateRows, out message))
        {
            return false;
        }

        bool[,] enclosedArea = FindEnclosedArea(sketch);
        int sketchBoxCount = 0;
        int sketchTargetCount = 0;
        int candidateBoxCount = 0;
        int candidateTargetCount = 0;
        int playerCount = 0;
        int internalWallTileCount = 0;
        HashSet<Vector2Int> generatedInternalWalls =
            new HashSet<Vector2Int>();

        for (int y = 0; y < sketch.height; y++)
        {
            for (int x = 0; x < sketch.width; x++)
            {
                char source = sketch.rows[y][x];
                char candidate = candidateRows[y][x];

                if (!IsAllowedCandidateTile(candidate))
                {
                    message = "Candidate contains an unsupported tile at "
                        + FormatPosition(x, y)
                        + ".";
                    return false;
                }

                if ((source == LevelData.Wall
                        || source == LevelData.Box
                        || source == LevelData.Target)
                    && candidate != source)
                {
                    message = "Candidate changed a fixed sketch tile at "
                        + FormatPosition(x, y)
                        + ".";
                    return false;
                }

                if (!enclosedArea[x, y] && candidate != source)
                {
                    message = "Candidate changed a tile outside the enclosed activity area at "
                        + FormatPosition(x, y)
                        + ".";
                    return false;
                }

                if (enclosedArea[x, y]
                    && source == LevelData.Empty
                    && candidate != LevelData.Ground
                    && candidate != LevelData.Wall
                    && candidate != LevelData.Water
                    && candidate != LevelData.Player)
                {
                    message = "Candidate left an incomplete activity tile at "
                        + FormatPosition(x, y)
                        + ".";
                    return false;
                }

                CountTile(source, ref sketchBoxCount, ref sketchTargetCount, ref playerCount, false);
                CountTile(
                    candidate,
                    ref candidateBoxCount,
                    ref candidateTargetCount,
                    ref playerCount,
                    true
                );

                if (candidate == LevelData.Wall
                    && source != LevelData.Wall)
                {
                    internalWallTileCount++;
                    generatedInternalWalls.Add(new Vector2Int(x, y));
                }
            }
        }

        if (candidateBoxCount != sketchBoxCount
            || candidateTargetCount != sketchTargetCount)
        {
            message = "Candidate added or removed box starts or targets.";
            return false;
        }

        if (playerCount != 1)
        {
            message = "Candidate must contain exactly one player start.";
            return false;
        }

        if (ContainsTwoByTwoBlock(generatedInternalWalls))
        {
            message = "Generated internal walls must not contain a complete "
                + "2x2 block.";
            return false;
        }

        if (!TryValidateCompetitionWallRule(
                generatedInternalWalls,
                competitionMode,
                out message))
        {
            return false;
        }

        if (!PCDesignFeasibilityValidator.TryValidateStartClearance(
                candidateRows,
                out message))
        {
            return false;
        }

        if (!TryValidateWaterAreas(
                candidateRows,
                sketch.width,
                sketch.height,
                minimumWaterWidth,
                minimumWaterHeight,
                maximumWaterWidth,
                maximumWaterHeight,
                out int waterAreaCount,
                out message))
        {
            return false;
        }

        if (internalWallTileCount < minimumInternalWallTiles)
        {
            message = "Candidate needs at least "
                + minimumInternalWallTiles
                + " generated internal wall tiles.";
            return false;
        }

        if (waterAreaCount < minimumWaterAreas)
        {
            message = "Candidate needs at least "
                + minimumWaterAreas
                + " water area.";
            return false;
        }

        if (!TryValidateConnectedActivityArea(
                candidateRows,
                sketch.width,
                sketch.height,
                minimumActivityArea,
                out message))
        {
            return false;
        }

        message = "Candidate structure is valid.";
        return true;
    }

    private static bool TryValidateDimensions(
        PCDesignSketchData sketch,
        string[] candidateRows,
        out string message)
    {
        if (sketch == null
            || sketch.width != 12
            || sketch.height != 10
            || sketch.rows == null
            || sketch.rows.Length != sketch.height)
        {
            message = "Saved PC design is missing or has an unsupported size.";
            return false;
        }

        if (candidateRows == null || candidateRows.Length != sketch.height)
        {
            message = "Candidate must contain exactly 10 rows.";
            return false;
        }

        for (int y = 0; y < sketch.height; y++)
        {
            if (sketch.rows[y] == null
                || sketch.rows[y].Length != sketch.width
                || candidateRows[y] == null
                || candidateRows[y].Length != sketch.width)
            {
                message = "Every sketch and candidate row must contain exactly 12 cells.";
                return false;
            }
        }

        message = "";
        return true;
    }

    private static bool[,] FindEnclosedArea(PCDesignSketchData sketch)
    {
        bool[,] outside = new bool[sketch.width, sketch.height];
        Queue<Vector2Int> open = new Queue<Vector2Int>();

        for (int x = 0; x < sketch.width; x++)
        {
            EnqueueSketchGround(sketch, new Vector2Int(x, 0), outside, open);
            EnqueueSketchGround(
                sketch,
                new Vector2Int(x, sketch.height - 1),
                outside,
                open
            );
        }

        for (int y = 0; y < sketch.height; y++)
        {
            EnqueueSketchGround(sketch, new Vector2Int(0, y), outside, open);
            EnqueueSketchGround(
                sketch,
                new Vector2Int(sketch.width - 1, y),
                outside,
                open
            );
        }

        while (open.Count > 0)
        {
            Vector2Int current = open.Dequeue();

            for (int direction = 0; direction < directions.Length; direction++)
            {
                EnqueueSketchGround(
                    sketch,
                    current + directions[direction],
                    outside,
                    open
                );
            }
        }

        bool[,] enclosed = new bool[sketch.width, sketch.height];

        for (int y = 0; y < sketch.height; y++)
        {
            for (int x = 0; x < sketch.width; x++)
            {
                enclosed[x, y] = sketch.rows[y][x] != LevelData.Wall
                    && !outside[x, y];
            }
        }

        return enclosed;
    }

    private static void EnqueueSketchGround(
        PCDesignSketchData sketch,
        Vector2Int position,
        bool[,] visited,
        Queue<Vector2Int> open)
    {
        if (position.x < 0
            || position.x >= sketch.width
            || position.y < 0
            || position.y >= sketch.height
            || visited[position.x, position.y]
            || sketch.rows[position.y][position.x] == LevelData.Wall)
        {
            return;
        }

        visited[position.x, position.y] = true;
        open.Enqueue(position);
    }

    private static bool TryValidateWaterAreas(
        string[] rows,
        int width,
        int height,
        int minimumWidth,
        int minimumHeight,
        int maximumWidth,
        int maximumHeight,
        out int waterAreaCount,
        out string message)
    {
        waterAreaCount = 0;
        bool[,] visited = new bool[width, height];

        for (int y = 0; y < height; y++)
        {
            for (int x = 0; x < width; x++)
            {
                if (rows[y][x] != LevelData.Water)
                {
                    continue;
                }

                if (y > 0 && rows[y - 1][x] == LevelData.Wall)
                {
                    message = "Water at row "
                        + (y + 1)
                        + ", column "
                        + (x + 1)
                        + " cannot have a wall directly above it.";
                    return false;
                }

                if (visited[x, y])
                {
                    continue;
                }

                waterAreaCount++;
                Queue<Vector2Int> open = new Queue<Vector2Int>();
                open.Enqueue(new Vector2Int(x, y));
                visited[x, y] = true;
                int minX = x;
                int maxX = x;
                int minY = y;
                int maxY = y;
                int area = 0;

                while (open.Count > 0)
                {
                    Vector2Int current = open.Dequeue();
                    area++;
                    minX = Mathf.Min(minX, current.x);
                    maxX = Mathf.Max(maxX, current.x);
                    minY = Mathf.Min(minY, current.y);
                    maxY = Mathf.Max(maxY, current.y);

                    for (int direction = 0; direction < directions.Length; direction++)
                    {
                        Vector2Int next = current + directions[direction];

                        if (next.x < 0
                            || next.x >= width
                            || next.y < 0
                            || next.y >= height
                            || visited[next.x, next.y]
                            || rows[next.y][next.x] != LevelData.Water)
                        {
                            continue;
                        }

                        visited[next.x, next.y] = true;
                        open.Enqueue(next);
                    }
                }

                int areaWidth = maxX - minX + 1;
                int areaHeight = maxY - minY + 1;

                if (area != areaWidth * areaHeight
                    || areaWidth < minimumWidth
                    || areaHeight < minimumHeight
                    || areaWidth > maximumWidth
                    || areaHeight > maximumHeight)
                {
                    message = "Each water area must be a complete "
                        + minimumWidth
                        + "-"
                        + maximumWidth
                        + " by "
                        + minimumHeight
                        + "-"
                        + maximumHeight
                        + " rectangle.";
                    return false;
                }
            }
        }

        message = "";
        return true;
    }

    private static bool ContainsTwoByTwoBlock(
        HashSet<Vector2Int> positions)
    {
        foreach (Vector2Int position in positions)
        {
            if (positions.Contains(
                    new Vector2Int(position.x + 1, position.y))
                && positions.Contains(
                    new Vector2Int(position.x, position.y + 1))
                && positions.Contains(
                    new Vector2Int(position.x + 1, position.y + 1)))
            {
                return true;
            }
        }

        return false;
    }

    private static bool TryValidateCompetitionWallRule(
        HashSet<Vector2Int> positions,
        string competitionMode,
        out string message)
    {
        if (!CompetitionModeController.IsValidMode(competitionMode))
        {
            message = "Competition mode is missing or unsupported.";
            return false;
        }

        List<int> componentSizes = GetComponentSizes(positions);

        if (competitionMode == CompetitionModeController.CompetitiveModeId
            && componentSizes.Exists(size => size > 2))
        {
            message = "Competitive mode requires every generated internal wall "
                + "group to contain at most two connected tiles.";
            return false;
        }

        if (competitionMode == CompetitionModeController.SupportiveModeId
            && componentSizes.Count > 1)
        {
            message = "Supportive mode requires all generated internal wall "
                + "tiles to be connected.";
            return false;
        }

        message = "";
        return true;
    }

    private static List<int> GetComponentSizes(
        HashSet<Vector2Int> positions)
    {
        List<int> sizes = new List<int>();
        HashSet<Vector2Int> remaining =
            new HashSet<Vector2Int>(positions);

        while (remaining.Count > 0)
        {
            Vector2Int start = default(Vector2Int);

            foreach (Vector2Int position in remaining)
            {
                start = position;
                break;
            }

            Queue<Vector2Int> open = new Queue<Vector2Int>();
            open.Enqueue(start);
            remaining.Remove(start);
            int size = 0;

            while (open.Count > 0)
            {
                Vector2Int current = open.Dequeue();
                size++;

                for (int direction = 0; direction < directions.Length; direction++)
                {
                    Vector2Int next = current + directions[direction];

                    if (remaining.Remove(next))
                    {
                        open.Enqueue(next);
                    }
                }
            }

            sizes.Add(size);
        }

        return sizes;
    }

    private static bool TryValidateConnectedActivityArea(
        string[] rows,
        int width,
        int height,
        int minimumActivityArea,
        out string message)
    {
        int activityArea = 0;
        Vector2Int start = Vector2Int.zero;
        bool hasStart = false;

        for (int y = 0; y < height; y++)
        {
            for (int x = 0; x < width; x++)
            {
                if (!LevelData.IsGround(rows[y][x]))
                {
                    continue;
                }

                activityArea++;

                if (!hasStart)
                {
                    start = new Vector2Int(x, y);
                    hasStart = true;
                }
            }
        }

        if (activityArea < minimumActivityArea)
        {
            message = "Candidate activity area needs at least "
                + minimumActivityArea
                + " walkable cells.";
            return false;
        }

        bool[,] visited = new bool[width, height];
        Queue<Vector2Int> open = new Queue<Vector2Int>();
        open.Enqueue(start);
        visited[start.x, start.y] = true;
        int connectedArea = 0;

        while (open.Count > 0)
        {
            Vector2Int current = open.Dequeue();
            connectedArea++;

            for (int direction = 0; direction < directions.Length; direction++)
            {
                Vector2Int next = current + directions[direction];

                if (next.x < 0
                    || next.x >= width
                    || next.y < 0
                    || next.y >= height
                    || visited[next.x, next.y]
                    || !LevelData.IsGround(rows[next.y][next.x]))
                {
                    continue;
                }

                visited[next.x, next.y] = true;
                open.Enqueue(next);
            }
        }

        if (connectedArea != activityArea)
        {
            message = "Candidate walkable cells must form one connected activity area.";
            return false;
        }

        message = "";
        return true;
    }

    private static bool IsAllowedCandidateTile(char tile)
    {
        return tile == LevelData.Empty
            || tile == LevelData.Ground
            || tile == LevelData.Wall
            || tile == LevelData.Water
            || tile == LevelData.Player
            || tile == LevelData.Box
            || tile == LevelData.Target;
    }

    private static void CountTile(
        char tile,
        ref int boxCount,
        ref int targetCount,
        ref int playerCount,
        bool countPlayer)
    {
        if (tile == LevelData.Box)
        {
            boxCount++;
        }
        else if (tile == LevelData.Target)
        {
            targetCount++;
        }
        else if (countPlayer && tile == LevelData.Player)
        {
            playerCount++;
        }
    }

    private static string FormatPosition(int x, int y)
    {
        return "(" + x + ", " + y + ")";
    }
}
