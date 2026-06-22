using System.Collections.Generic;
using UnityEngine;

[DefaultExecutionOrder(-100)]
public class LevelGenerator : MonoBehaviour
{
    [Header("References")]
    public LevelData levelData;
    public LevelLoader levelLoader;
    public LevelSolver levelSolver;
    public LevelGenerationRules rules;

    [Header("Runtime")]
    public bool generateOnAwake = true;
    public bool logGenerationResult = true;

    [Header("Last Result")]
    public int lastAttempts;
    public int lastReversePulls;
    public int lastSearchedStates;
    public int lastSolutionSteps;

    private const char Ground = '.';
    private const char Wall = '#';
    private const char Water = '@';
    private const char Player = 'p';
    private const char Box = 's';
    private const char Target = 't';

    private static readonly Vector2Int[] directions =
    {
        new Vector2Int(0, -1),
        new Vector2Int(0, 1),
        new Vector2Int(-1, 0),
        new Vector2Int(1, 0)
    };

    private readonly List<Vector2Int> boxes = new List<Vector2Int>();
    private readonly List<Vector2Int> targets = new List<Vector2Int>();
    private readonly HashSet<Vector2Int> targetLookup = new HashSet<Vector2Int>();
    private System.Random random;
    private Vector2Int playerPosition;

    private void Awake()
    {
        if (generateOnAwake)
        {
            Generate();
        }
    }

    [ContextMenu("Generate And Reload")]
    public void GenerateAndReload()
    {
        if (!Generate())
        {
            return;
        }

        if (levelLoader != null)
        {
            levelLoader.levelData = levelData;
            levelLoader.LoadLevel();
        }
    }

    public bool Generate()
    {
        ResolveReferences();

        if (!CanGenerate())
        {
            return false;
        }

        random = rules.useFixedSeed
            ? new System.Random(rules.seed)
            : new System.Random();

        for (int attempt = 1; attempt <= rules.maxGenerateAttempts; attempt++)
        {
            if (!TryCreateCandidate(out string[] rows, out int reversePulls))
            {
                continue;
            }

            levelData.rows = rows;
            levelSolver.levelData = levelData;

            if (!levelSolver.ParseLevel())
            {
                continue;
            }

            if (!levelSolver.CanSolve(out int searchedStates, out int solutionSteps))
            {
                continue;
            }

            lastAttempts = attempt;
            lastReversePulls = reversePulls;
            lastSearchedStates = searchedStates;
            lastSolutionSteps = solutionSteps;

            if (levelLoader != null)
            {
                levelLoader.levelData = levelData;
            }

            if (logGenerationResult)
            {
                Debug.Log(
                    "LevelGenerator generated level:"
                    + " attempts=" + lastAttempts
                    + ", reversePulls=" + lastReversePulls
                    + ", searchedStates=" + lastSearchedStates
                    + ", solutionSteps=" + lastSolutionSteps
                );
            }

            return true;
        }

        Debug.LogWarning("LevelGenerator: Failed to generate a solvable level.");
        return false;
    }

    private void ResolveReferences()
    {
        if (rules == null)
        {
            rules = FindObjectOfType<LevelGenerationRules>();
        }

        if (levelLoader == null)
        {
            levelLoader = FindObjectOfType<LevelLoader>();
        }

        if (levelData == null && levelLoader != null)
        {
            levelData = levelLoader.levelData;
        }

        if (levelData == null)
        {
            levelData = FindObjectOfType<LevelData>();
        }

        if (levelSolver == null)
        {
            levelSolver = FindObjectOfType<LevelSolver>();
        }

        if (levelSolver == null)
        {
            levelSolver = gameObject.AddComponent<LevelSolver>();
            levelSolver.parseOnStart = false;
            levelSolver.solveOnStart = false;
        }
    }

    private bool CanGenerate()
    {
        if (rules == null)
        {
            Debug.LogWarning("LevelGenerator: LevelGenerationRules is missing.");
            return false;
        }

        if (levelData == null)
        {
            Debug.LogWarning("LevelGenerator: LevelData is missing.");
            return false;
        }

        if (!rules.IsValid())
        {
            Debug.LogWarning("LevelGenerator: LevelGenerationRules contains invalid values.");
            return false;
        }

        int playableCells = (rules.width - 2) * (rules.height - 2);
        int minimumRequiredCells = rules.boxCount * 2 + 1;

        if (playableCells < minimumRequiredCells)
        {
            Debug.LogWarning("LevelGenerator: Map is too small for the requested box count.");
            return false;
        }

        return true;
    }

    private bool TryCreateCandidate(out string[] rows, out int reversePulls)
    {
        rows = null;
        reversePulls = 0;

        char[,] grid = CreateBaseGrid();
        AddWaterAreas(grid);

        if (!TryPlaceTargets(grid))
        {
            return false;
        }

        if (!TryPlacePlayer(grid))
        {
            return false;
        }

        int targetPulls = NextInclusive(rules.minReversePulls, rules.maxReversePulls);

        if (!TryReversePulls(grid, targetPulls, out reversePulls))
        {
            return false;
        }

        return TryBuildRows(grid, out rows);
    }

    private char[,] CreateBaseGrid()
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

    private void AddWaterAreas(char[,] grid)
    {
        int waterAreaCount = random.Next(rules.maxWaterAreas + 1);

        for (int i = 0; i < waterAreaCount; i++)
        {
            TryAddWaterArea(grid);
        }
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

            int minOriginY = 2;
            int maxOriginX = rules.width - size.x - 1;
            int maxOriginY = rules.height - size.y - 1;

            if (maxOriginX < 1 || maxOriginY < minOriginY)
            {
                continue;
            }

            Vector2Int origin = new Vector2Int(
                NextInclusive(1, maxOriginX),
                NextInclusive(minOriginY, maxOriginY)
            );

            if (!rules.IsValidWaterRect(origin, size) || !IsRectClear(grid, origin, size))
            {
                continue;
            }

            for (int y = origin.y; y < origin.y + size.y; y++)
            {
                for (int x = origin.x; x < origin.x + size.x; x++)
                {
                    grid[x, y] = Water;
                }
            }

            return true;
        }

        return false;
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

    private bool TryPlacePlayer(char[,] grid)
    {
        List<Vector2Int> candidates = GetGroundCells(grid);
        Shuffle(candidates);

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

    private bool TryReversePulls(char[,] grid, int targetPulls, out int reversePulls)
    {
        reversePulls = 0;
        bool[] movedBoxes = new bool[boxes.Count];

        for (int attempt = 0; attempt < rules.maxReverseStepAttempts && reversePulls < targetPulls; attempt++)
        {
            int boxIndex = random.Next(boxes.Count);
            Vector2Int direction = directions[random.Next(directions.Length)];

            if (!TryReversePull(grid, boxIndex, direction))
            {
                continue;
            }

            movedBoxes[boxIndex] = true;
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

    private bool TryReversePull(char[,] grid, int boxIndex, Vector2Int direction)
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

        boxes[boxIndex] = nextBoxPosition;
        playerPosition = nextPlayerPosition;
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

    private bool CanPlayerOccupy(char[,] grid, Vector2Int position)
    {
        return rules.IsInsidePlayableArea(position)
            && grid[position.x, position.y] != Wall
            && grid[position.x, position.y] != Water;
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

    private void Shuffle(List<Vector2Int> positions)
    {
        for (int i = positions.Count - 1; i > 0; i--)
        {
            int swapIndex = random.Next(i + 1);
            Vector2Int value = positions[i];
            positions[i] = positions[swapIndex];
            positions[swapIndex] = value;
        }
    }

    private int NextInclusive(int min, int max)
    {
        return random.Next(min, max + 1);
    }
}
