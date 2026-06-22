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
    private const char Empty = ' ';
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

    private static readonly string[][] wallObstacleShapes =
    {
        new string[]
        {
            "##",
            "#."
        },
        new string[]
        {
            "##",
            "#.",
            "#."
        },
        new string[]
        {
            "###",
            ".#."
        },
        new string[]
        {
            "##.",
            ".##"
        },
        new string[]
        {
            "#.#",
            "###"
        },
        new string[]
        {
            "##.",
            "#..",
            "##."
        },
        new string[]
        {
            "###",
            "#.."
        },
        new string[]
        {
            "##",
            ".#",
            ".#"
        }
    };

    private static readonly string[][] supportedOuterShellTemplates =
    {
        new string[]
        {
            "  ########  ",
            " ##......## ",
            " #........# ",
            "##........##",
            "#..........#",
            "#..........#",
            "##........##",
            " #........# ",
            " ##......## ",
            "  ########  "
        },
        new string[]
        {
            " ###########",
            "##.........#",
            "#..........#",
            "#..........#",
            "#.........##",
            "#........## ",
            "##.......#  ",
            " ##......#  ",
            "  #......#  ",
            "  ########  "
        },
        new string[]
        {
            "   #######  ",
            "  ##.....## ",
            " ##.......##",
            " #.........#",
            "##.........#",
            "#..........#",
            "#.........##",
            "##.......## ",
            " ##.....##  ",
            "  #######   "
        }
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

            if (solutionSteps < rules.minSolutionSteps || solutionSteps > rules.maxSolutionSteps)
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

        if (!TryCreateBaseGrid(out char[,] grid))
        {
            return false;
        }

        if (!TryAddWaterAreas(grid))
        {
            return false;
        }

        if (!TryAddWallObstacleBlocks(grid))
        {
            return false;
        }

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

        if (!ValidateTileRules(grid, true))
        {
            return false;
        }

        return TryBuildRows(grid, out rows);
    }

    private bool TryCreateBaseGrid(out char[,] grid)
    {
        grid = rules.enableIrregularOuterWalls
            ? CreateSupportedOuterShell()
            : CreateRectangularOuterShell();

        return HasEnoughGroundCells(grid)
            && AreGroundCellsConnected(grid)
            && ValidateWallTileRules(grid);
    }

    private char[,] CreateSupportedOuterShell()
    {
        if (rules.width != 12 || rules.height != 10)
        {
            return CreateRectangularOuterShell();
        }

        string[] template = supportedOuterShellTemplates[random.Next(supportedOuterShellTemplates.Length)];
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

            if (!rules.IsValidWaterRect(origin, size) || !CanPlaceWaterRect(grid, origin, size))
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

    private bool TryAddWallObstacleBlocks(char[,] grid)
    {
        if (!HasEnoughGroundCells(grid) || !AreGroundCellsConnected(grid) || !ValidateWallTileRules(grid))
        {
            return false;
        }

        int obstacleCount = NextInclusive(rules.minWallObstacleBlocks, rules.maxWallObstacleBlocks);
        int placedObstacles = 0;
        int maxAttempts = Mathf.Max(80, obstacleCount * 50);

        for (int attempt = 0; attempt < maxAttempts && placedObstacles < obstacleCount; attempt++)
        {
            if (TryAddWallObstacleBlock(grid))
            {
                placedObstacles++;
            }
        }

        return placedObstacles >= obstacleCount;
    }

    private bool TryAddWallObstacleBlock(char[,] grid)
    {
        string[] shape = wallObstacleShapes[random.Next(wallObstacleShapes.Length)];
        Vector2Int size = GetWallObstacleShapeSize(shape);
        int minX = 2;
        int minY = 2;
        int maxX = rules.width - size.x - 2;
        int maxY = rules.height - size.y - 2;

        if (maxX < minX || maxY < minY)
        {
            return false;
        }

        Vector2Int origin = new Vector2Int(
            NextInclusive(minX, maxX),
            NextInclusive(minY, maxY)
        );

        if (!CanPlaceWallObstacleShape(grid, origin, shape))
        {
            return false;
        }

        SetWallObstacleShape(grid, origin, shape, Wall);

        if (!HasEnoughGroundCells(grid)
            || !AreGroundCellsConnected(grid)
            || !ValidateWallTileRules(grid))
        {
            SetWallObstacleShape(grid, origin, shape, Ground);
            return false;
        }

        return true;
    }

    private bool CanPlaceWallObstacleShape(char[,] grid, Vector2Int origin, string[] shape)
    {
        Vector2Int size = GetWallObstacleShapeSize(shape);

        for (int y = origin.y - 1; y <= origin.y + size.y; y++)
        {
            for (int x = origin.x - 1; x <= origin.x + size.x; x++)
            {
                Vector2Int position = new Vector2Int(x, y);

                if (!rules.IsInsidePlayableArea(position) || grid[x, y] != Ground)
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

                if (HasWaterBelow(grid, new Vector2Int(x, y)))
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

                if (runLength >= 2)
                {
                    return RejectParallelWallRows(runStartX, y, runLength, logFailure);
                }

                runStartX = -1;
                runLength = 0;
            }

            if (runLength >= 2)
            {
                return RejectParallelWallRows(runStartX, y, runLength, logFailure);
            }
        }

        return true;
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
        if (IsSurroundedWallShape(grid, position))
        {
            return true;
        }

        bool up = IsWallAt(grid, position + new Vector2Int(0, -1));
        bool down = IsWallAt(grid, position + new Vector2Int(0, 1));
        bool left = IsWallAt(grid, position + new Vector2Int(-1, 0));
        bool right = IsWallAt(grid, position + new Vector2Int(1, 0));
        bool rightDown = IsWallAt(grid, position + new Vector2Int(1, 1));

        if (up && (left || right))
        {
            return true;
        }

        if (right && rightDown)
        {
            return true;
        }

        if (up || down)
        {
            return true;
        }

        return left && right;
    }

    private bool IsSurroundedWallShape(char[,] grid, Vector2Int position)
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
