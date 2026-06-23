using System.Collections;
using System.Collections.Generic;
using System.Text;
using UnityEngine;

// LevelSolver 当前用于判断 LevelData 地图是否可以通关。
//
// 主要输出结果：
// - ParseAndLog()：只解析地图，并在 Console 输出玩家、箱子、终点、障碍数量。
// - SolveAndLog()：解析并尝试求解地图，然后在 Console 输出求解结果。
// - CanSolve(out searchedStates, out solutionSteps)：真正用于判断地图是否有解。
//
// CanSolve 的返回值：
// - true：地图有解，所有箱子可以被推到目标点。
// - false：地图无解，或者搜索超过 maxSearchStates，或者地图数据不合法。
//
// CanSolve 的输出参数：
// - searchedStates：本次 BFS 搜索过的状态数量，可用于判断搜索开销。
// - solutionSteps：找到解时的移动步数；如果没找到解，值为 -1。
//
// SolveAndLog 的 Console 输出示例：
// LevelSolver solved level: solvable=True, searchedStates=1234, solutionSteps=56
//
// 其中 solvable 就是最终是否可解的判断结果。
public class LevelSolver : MonoBehaviour
{
    public LevelData levelData;
    public bool parseOnStart = true;
    public bool solveOnStart = true;
    public int maxSearchStates = 50000;

    private Vector2Int playerPos;
    private bool hasPlayer;
    private readonly List<Vector2Int> boxes = new List<Vector2Int>();
    private readonly HashSet<Vector2Int> targets = new HashSet<Vector2Int>();
    private readonly HashSet<Vector2Int> blocks = new HashSet<Vector2Int>();
    private static readonly Vector2Int[] directions =
    {
        new Vector2Int(0, -1),
        new Vector2Int(0, 1),
        new Vector2Int(-1, 0),
        new Vector2Int(1, 0)
    };

    public Vector2Int PlayerPos
    {
        get { return playerPos; }
    }

    public int BoxCount
    {
        get { return boxes.Count; }
    }

    public int TargetCount
    {
        get { return targets.Count; }
    }

    public int BlockCount
    {
        get { return blocks.Count; }
    }

    private void Start()
    {
        if (solveOnStart)
        {
            SolveAndLog();
        }
        else if (parseOnStart)
        {
            ParseAndLog();
        }
    }

    [ContextMenu("Parse And Log")]
    public void ParseAndLog()
    {
        if (ParseLevel())
        {
            LogParseResult();
        }
    }

    public bool ParseLevel()
    {
        ClearParsedData();

        if (levelData == null)
        {
            levelData = FindObjectOfType<LevelData>();
        }

        if (levelData == null || levelData.rows == null)
        {
            Debug.LogWarning("LevelSolver: LevelData is missing.");
            return false;
        }

        for (int y = 0; y < levelData.rows.Length; y++)
        {
            string row = levelData.rows[y];

            for (int x = 0; x < row.Length; x++)
            {
                ParseTile(row[x], new Vector2Int(x, y));
            }
        }

        ValidateParsedData();
        return true;
    }

    [ContextMenu("Solve And Log")]
    public void SolveAndLog()
    {
        if (!ParseLevel())
        {
            return;
        }

        bool solved = CanSolve(out int searchedStates, out int solutionSteps, out int pushCount);

        Debug.Log(
            "LevelSolver solved level:"
            + " solvable=" + solved
            + ", searchedStates=" + searchedStates
            + ", solutionSteps=" + solutionSteps
            + ", pushCount=" + pushCount
        );
    }

    public bool CanSolve(out int searchedStates, out int solutionSteps)
    {
        return CanSolve(out searchedStates, out solutionSteps, out _);
    }

    public bool CanSolve(out int searchedStates, out int solutionSteps, out int pushCount)
    {
        searchedStates = 0;
        solutionSteps = -1;
        pushCount = -1;

        if (!CanStartSolving())
        {
            return false;
        }

        List<Vector2Int> startBoxes = GetSortedBoxes(boxes);
        SolverState startState = new SolverState(playerPos, startBoxes, 0, 0);
        Queue<SolverState> openStates = new Queue<SolverState>();
        HashSet<string> visitedStates = new HashSet<string>();

        openStates.Enqueue(startState);
        visitedStates.Add(GetStateKey(startState.player, startState.boxes));

        while (openStates.Count > 0 && searchedStates < maxSearchStates)
        {
            SolverState current = openStates.Dequeue();
            searchedStates++;

            if (IsSolved(current.boxes))
            {
                solutionSteps = current.steps;
                pushCount = current.pushes;
                return true;
            }

            for (int i = 0; i < directions.Length; i++)
            {
                TryAddNextState(current, directions[i], openStates, visitedStates);
            }
        }

        if (searchedStates >= maxSearchStates)
        {
            Debug.LogWarning("LevelSolver: Search stopped because maxSearchStates was reached.");
        }

        return false;
    }

    private void TryAddNextState(
        SolverState current,
        Vector2Int direction,
        Queue<SolverState> openStates,
        HashSet<string> visitedStates)
    {
        Vector2Int nextPlayer = current.player + direction;
        int boxIndex = current.boxes.IndexOf(nextPlayer);

        if (boxIndex >= 0)
        {
            Vector2Int nextBox = nextPlayer + direction;

            if (!CanBoxMoveTo(nextBox, current.boxes))
            {
                return;
            }

            List<Vector2Int> nextBoxes = new List<Vector2Int>(current.boxes);
            nextBoxes[boxIndex] = nextBox;
            nextBoxes = GetSortedBoxes(nextBoxes);

            AddState(nextPlayer, nextBoxes, current.steps + 1, current.pushes + 1, openStates, visitedStates);
        }
        else
        {
            if (!CanPlayerMoveTo(nextPlayer, current.boxes))
            {
                return;
            }

            AddState(nextPlayer, current.boxes, current.steps + 1, current.pushes, openStates, visitedStates);
        }
    }

    private void AddState(
        Vector2Int player,
        List<Vector2Int> stateBoxes,
        int steps,
        int pushes,
        Queue<SolverState> openStates,
        HashSet<string> visitedStates)
    {
        string key = GetStateKey(player, stateBoxes);

        if (visitedStates.Contains(key))
        {
            return;
        }

        visitedStates.Add(key);
        openStates.Enqueue(new SolverState(player, stateBoxes, steps, pushes));
    }

    private bool CanStartSolving()
    {
        if (!hasPlayer || boxes.Count == 0 || targets.Count == 0 || boxes.Count != targets.Count)
        {
            Debug.LogWarning("LevelSolver: Cannot solve because parsed level data is invalid.");
            return false;
        }

        return true;
    }

    private bool CanPlayerMoveTo(Vector2Int position, List<Vector2Int> stateBoxes)
    {
        return IsWalkable(position) && !stateBoxes.Contains(position);
    }

    private bool CanBoxMoveTo(Vector2Int position, List<Vector2Int> stateBoxes)
    {
        return IsWalkable(position) && !stateBoxes.Contains(position);
    }

    private bool IsWalkable(Vector2Int position)
    {
        char tile = GetMapTile(position);
        return tile != '\0' && tile != ' ' && !blocks.Contains(position);
    }

    private bool IsSolved(List<Vector2Int> stateBoxes)
    {
        for (int i = 0; i < stateBoxes.Count; i++)
        {
            if (!targets.Contains(stateBoxes[i]))
            {
                return false;
            }
        }

        return true;
    }

    private List<Vector2Int> GetSortedBoxes(List<Vector2Int> sourceBoxes)
    {
        List<Vector2Int> sortedBoxes = new List<Vector2Int>(sourceBoxes);
        sortedBoxes.Sort(ComparePositions);
        return sortedBoxes;
    }

    private int ComparePositions(Vector2Int a, Vector2Int b)
    {
        int yCompare = a.y.CompareTo(b.y);

        if (yCompare != 0)
        {
            return yCompare;
        }

        return a.x.CompareTo(b.x);
    }

    private string GetStateKey(Vector2Int player, List<Vector2Int> stateBoxes)
    {
        StringBuilder builder = new StringBuilder();
        builder.Append(player.x);
        builder.Append(',');
        builder.Append(player.y);
        builder.Append('|');

        for (int i = 0; i < stateBoxes.Count; i++)
        {
            builder.Append(stateBoxes[i].x);
            builder.Append(',');
            builder.Append(stateBoxes[i].y);
            builder.Append(';');
        }

        return builder.ToString();
    }

    private void ParseTile(char tile, Vector2Int position)
    {
        if (tile == 'p')
        {
            if (hasPlayer)
            {
                Debug.LogWarning("LevelSolver: More than one player start was found.");
            }

            hasPlayer = true;
            playerPos = position;
        }
        else if (tile == 's')
        {
            boxes.Add(position);
        }
        else if (tile == 't')
        {
            targets.Add(position);
        }
        else if (tile == '#' || tile == '@')
        {
            blocks.Add(position);
        }
    }

    private void ClearParsedData()
    {
        playerPos = Vector2Int.zero;
        hasPlayer = false;
        boxes.Clear();
        targets.Clear();
        blocks.Clear();
    }

    private void ValidateParsedData()
    {
        if (!hasPlayer)
        {
            Debug.LogWarning("LevelSolver: No player start found. Add one 'p' to LevelData.");
        }

        if (boxes.Count == 0)
        {
            Debug.LogWarning("LevelSolver: No boxes found. Add at least one 's' to LevelData.");
        }

        if (targets.Count == 0)
        {
            Debug.LogWarning("LevelSolver: No targets found. Add at least one 't' to LevelData.");
        }

        if (boxes.Count != targets.Count)
        {
            Debug.LogWarning("LevelSolver: Box count and target count do not match.");
        }
    }

    private void LogParseResult()
    {
        Debug.Log(
            "LevelSolver parsed level:"
            + " player=" + (hasPlayer ? playerPos.ToString() : "missing")
            + ", boxes=" + boxes.Count
            + ", targets=" + targets.Count
            + ", blocks=" + blocks.Count
        );
    }

    private char GetMapTile(Vector2Int position)
    {
        if (levelData == null || levelData.rows == null)
        {
            return '\0';
        }

        if (position.y < 0 || position.y >= levelData.rows.Length)
        {
            return '\0';
        }

        string row = levelData.rows[position.y];

        if (position.x < 0 || position.x >= row.Length)
        {
            return '\0';
        }

        return row[position.x];
    }

    private class SolverState
    {
        public readonly Vector2Int player;
        public readonly List<Vector2Int> boxes;
        public readonly int steps;
        public readonly int pushes;

        public SolverState(Vector2Int player, List<Vector2Int> boxes, int steps, int pushes)
        {
            this.player = player;
            this.boxes = boxes;
            this.steps = steps;
            this.pushes = pushes;
        }
    }
}
