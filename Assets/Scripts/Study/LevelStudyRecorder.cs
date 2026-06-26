using System;
using System.Collections;
using System.Collections.Generic;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;

public class LevelStudyRecorder : MonoBehaviour
{
    private const string DefaultBackendBaseUrl = "https://surf-an4f.onrender.com";
    private const string LevelStartPath = "/record-level-start";
    private const string LevelEndPath = "/record-level-end";

    private static LevelStudyRecorder instance;

    [Header("Backend")]
    public string backendBaseUrl = DefaultBackendBaseUrl;
    public int requestTimeoutSeconds = 5;
    public bool logRecordEvents = true;

    private string sessionId;
    private string levelRunId;
    private int levelIndex;
    private float levelStartedAt;
    private int moveCount;
    private int pushCount;
    private int restartCount;
    private bool hasActiveLevel;
    private LevelLoader currentLevelLoader;

    public static LevelStudyRecorder Instance
    {
        get
        {
            if (instance == null)
            {
                instance = FindObjectOfType<LevelStudyRecorder>();
            }

            if (instance == null)
            {
                GameObject recorderObject = new GameObject("LevelStudyRecorder");
                instance = recorderObject.AddComponent<LevelStudyRecorder>();
            }

            return instance;
        }
    }

    private void Awake()
    {
        if (instance != null && instance != this)
        {
            Destroy(gameObject);
            return;
        }

        instance = this;
        DontDestroyOnLoad(gameObject);
        EnsureSessionId();
    }

    public static void RecordLevelStarted(LevelLoader levelLoader)
    {
        Instance.StartLevelRecord(levelLoader);
    }

    public static void RecordLevelCompleted()
    {
        Instance.EndLevelRecord(true, "completed");
    }

    public static void RecordLevelRestarted()
    {
        Instance.restartCount++;
        Instance.EndLevelRecord(false, "restarted");
    }

    public static void RecordPlayerMove(bool pushedBox)
    {
        Instance.RecordMove(pushedBox);
    }

    private void StartLevelRecord(LevelLoader levelLoader)
    {
        if (levelLoader == null || levelLoader.levelData == null || levelLoader.levelData.rows == null)
        {
            return;
        }

        EnsureSessionId();

        currentLevelLoader = levelLoader;
        levelRunId = Guid.NewGuid().ToString("N");
        levelIndex++;
        levelStartedAt = Time.realtimeSinceStartup;
        moveCount = 0;
        pushCount = 0;
        restartCount = 0;
        hasActiveLevel = true;

        LevelStartRecord record = CreateLevelStartRecord(levelLoader);
        SendRecord(LevelStartPath, record);

        if (logRecordEvents)
        {
            Debug.Log(
                "LevelStudyRecorder started level:"
                + " sessionId=" + sessionId
                + ", levelRunId=" + levelRunId
                + ", levelIndex=" + levelIndex
                + ", mapHash=" + record.structure.mapHash
                + ", source=" + record.source
            );
        }
    }

    private void EndLevelRecord(bool completed, string endReason)
    {
        if (!hasActiveLevel || string.IsNullOrEmpty(levelRunId))
        {
            return;
        }

        LevelEndRecord record = new LevelEndRecord
        {
            eventType = "level-end",
            sessionId = sessionId,
            levelRunId = levelRunId,
            levelIndex = levelIndex,
            completed = completed,
            endReason = endReason,
            durationSeconds = RoundFloat(Time.realtimeSinceStartup - levelStartedAt),
            moveCount = moveCount,
            pushCount = pushCount,
            restartCount = restartCount,
            timestamp = DateTime.UtcNow.ToString("o")
        };

        SendRecord(LevelEndPath, record);

        if (logRecordEvents)
        {
            Debug.Log(
                "LevelStudyRecorder ended level:"
                + " sessionId=" + sessionId
                + ", levelRunId=" + levelRunId
                + ", completed=" + completed
                + ", endReason=" + endReason
                + ", durationSeconds=" + record.durationSeconds
                + ", moveCount=" + moveCount
                + ", pushCount=" + pushCount
                + ", restartCount=" + restartCount
            );
        }

        hasActiveLevel = false;
    }

    private void RecordMove(bool pushedBox)
    {
        if (!hasActiveLevel)
        {
            return;
        }

        moveCount++;

        if (pushedBox)
        {
            pushCount++;
        }
    }

    private LevelStartRecord CreateLevelStartRecord(LevelLoader levelLoader)
    {
        LevelGenerator generator = levelLoader.levelGenerator;
        LevelStructureData structure = BuildStructureData(levelLoader.levelData.rows);

        return new LevelStartRecord
        {
            eventType = "level-start",
            sessionId = sessionId,
            levelRunId = levelRunId,
            levelIndex = levelIndex,
            source = levelLoader.GetCurrentLevelSource(),
            timestamp = DateTime.UtcNow.ToString("o"),
            solutionSteps = generator != null ? generator.lastSolutionSteps : -1,
            solverPushes = generator != null ? generator.lastPushes : -1,
            generationAttempts = generator != null ? generator.lastAttempts : -1,
            reversePulls = generator != null ? generator.lastReversePulls : -1,
            searchedStates = generator != null ? generator.lastSearchedStates : -1,
            rows = CloneRows(levelLoader.levelData.rows),
            structure = structure
        };
    }

    private LevelStructureData BuildStructureData(string[] rows)
    {
        LevelStructureData data = new LevelStructureData();

        if (rows == null || rows.Length == 0)
        {
            data.mapHash = "";
            return data;
        }

        data.height = rows.Length;
        data.width = GetMapWidth(rows);
        data.mapHash = GetMapHash(rows);

        List<Vector2Int> boxes = new List<Vector2Int>();
        List<Vector2Int> targets = new List<Vector2Int>();
        Vector2Int player = Vector2Int.zero;
        bool hasPlayer = false;

        for (int y = 0; y < rows.Length; y++)
        {
            string row = rows[y] ?? "";

            for (int x = 0; x < data.width; x++)
            {
                char tile = x < row.Length ? row[x] : ' ';

                if (tile == '#')
                {
                    data.wallCount++;
                }
                else if (tile == '@')
                {
                    data.waterCount++;
                }
                else if (tile == ' ')
                {
                    data.emptyCount++;
                }
                else
                {
                    data.floorCount++;
                }

                if (tile == 's')
                {
                    data.boxCount++;
                    boxes.Add(new Vector2Int(x, y));
                }
                else if (tile == 't')
                {
                    data.targetCount++;
                    targets.Add(new Vector2Int(x, y));
                }
                else if (tile == 'p')
                {
                    player = new Vector2Int(x, y);
                    hasPlayer = true;
                }
            }
        }

        int mapArea = Mathf.Max(1, data.width * data.height);
        data.wallDensity = RoundFloat((float)data.wallCount / mapArea);
        data.waterDensity = RoundFloat((float)data.waterCount / mapArea);
        data.reachableAreaRatio = RoundFloat(GetReachableAreaRatio(rows, data.width, hasPlayer, player));
        data.boxGoalDistanceAvg = RoundFloat(GetAverageNearestTargetDistance(boxes, targets));
        data.boxGoalDistanceMin = GetMinimumBoxTargetDistance(boxes, targets);
        data.deadCornerCount = CountDeadCorners(rows, data.width, targets);
        data.deadCornerRisk = RoundFloat(data.floorCount > 0 ? (float)data.deadCornerCount / data.floorCount : 0f);

        return data;
    }

    private float GetReachableAreaRatio(string[] rows, int width, bool hasPlayer, Vector2Int player)
    {
        int walkableCount = CountWalkableCells(rows, width);

        if (!hasPlayer || walkableCount == 0 || !IsWalkable(rows, width, player))
        {
            return 0f;
        }

        Queue<Vector2Int> open = new Queue<Vector2Int>();
        HashSet<Vector2Int> visited = new HashSet<Vector2Int>();

        open.Enqueue(player);
        visited.Add(player);

        while (open.Count > 0)
        {
            Vector2Int current = open.Dequeue();
            AddReachableNeighbor(rows, width, current + Vector2Int.up, open, visited);
            AddReachableNeighbor(rows, width, current + Vector2Int.down, open, visited);
            AddReachableNeighbor(rows, width, current + Vector2Int.left, open, visited);
            AddReachableNeighbor(rows, width, current + Vector2Int.right, open, visited);
        }

        return (float)visited.Count / walkableCount;
    }

    private void AddReachableNeighbor(
        string[] rows,
        int width,
        Vector2Int position,
        Queue<Vector2Int> open,
        HashSet<Vector2Int> visited)
    {
        if (visited.Contains(position) || !IsWalkable(rows, width, position))
        {
            return;
        }

        visited.Add(position);
        open.Enqueue(position);
    }

    private int CountWalkableCells(string[] rows, int width)
    {
        int count = 0;

        for (int y = 0; y < rows.Length; y++)
        {
            for (int x = 0; x < width; x++)
            {
                if (IsWalkable(rows, width, new Vector2Int(x, y)))
                {
                    count++;
                }
            }
        }

        return count;
    }

    private bool IsWalkable(string[] rows, int width, Vector2Int position)
    {
        char tile = GetTile(rows, width, position);
        return tile == '.' || tile == 'p' || tile == 's' || tile == 't';
    }

    private float GetAverageNearestTargetDistance(List<Vector2Int> boxes, List<Vector2Int> targets)
    {
        if (boxes.Count == 0 || targets.Count == 0)
        {
            return -1f;
        }

        int totalDistance = 0;

        for (int i = 0; i < boxes.Count; i++)
        {
            totalDistance += GetNearestTargetDistance(boxes[i], targets);
        }

        return (float)totalDistance / boxes.Count;
    }

    private int GetMinimumBoxTargetDistance(List<Vector2Int> boxes, List<Vector2Int> targets)
    {
        if (boxes.Count == 0 || targets.Count == 0)
        {
            return -1;
        }

        int bestDistance = int.MaxValue;

        for (int i = 0; i < boxes.Count; i++)
        {
            bestDistance = Mathf.Min(bestDistance, GetNearestTargetDistance(boxes[i], targets));
        }

        return bestDistance;
    }

    private int GetNearestTargetDistance(Vector2Int box, List<Vector2Int> targets)
    {
        int bestDistance = int.MaxValue;

        for (int i = 0; i < targets.Count; i++)
        {
            bestDistance = Mathf.Min(bestDistance, ManhattanDistance(box, targets[i]));
        }

        return bestDistance;
    }

    private int CountDeadCorners(string[] rows, int width, List<Vector2Int> targets)
    {
        int count = 0;

        for (int y = 0; y < rows.Length; y++)
        {
            for (int x = 0; x < width; x++)
            {
                Vector2Int position = new Vector2Int(x, y);

                if (!IsWalkable(rows, width, position) || targets.Contains(position))
                {
                    continue;
                }

                bool upBlocked = IsBlocking(rows, width, position + Vector2Int.up);
                bool downBlocked = IsBlocking(rows, width, position + Vector2Int.down);
                bool leftBlocked = IsBlocking(rows, width, position + Vector2Int.left);
                bool rightBlocked = IsBlocking(rows, width, position + Vector2Int.right);

                if ((upBlocked || downBlocked) && (leftBlocked || rightBlocked))
                {
                    count++;
                }
            }
        }

        return count;
    }

    private bool IsBlocking(string[] rows, int width, Vector2Int position)
    {
        char tile = GetTile(rows, width, position);
        return tile == '\0' || tile == ' ' || tile == '#' || tile == '@';
    }

    private char GetTile(string[] rows, int width, Vector2Int position)
    {
        if (rows == null
            || position.y < 0
            || position.y >= rows.Length
            || position.x < 0
            || position.x >= width)
        {
            return '\0';
        }

        string row = rows[position.y] ?? "";

        if (position.x >= row.Length)
        {
            return ' ';
        }

        return row[position.x];
    }

    private void SendRecord(string path, object record)
    {
        string json = JsonUtility.ToJson(record);
        StartCoroutine(PostJson(path, json));
    }

    private IEnumerator PostJson(string path, string json)
    {
        string url = GetBackendUrl(path);
        byte[] body = Encoding.UTF8.GetBytes(json);
        UnityWebRequest request = new UnityWebRequest(url, UnityWebRequest.kHttpVerbPOST);
        request.uploadHandler = new UploadHandlerRaw(body);
        request.downloadHandler = new DownloadHandlerBuffer();
        request.timeout = Mathf.Max(1, requestTimeoutSeconds);
        request.SetRequestHeader("Content-Type", "application/json");
        request.SetRequestHeader("Accept", "application/json");

        yield return request.SendWebRequest();

        if (request.result != UnityWebRequest.Result.Success)
        {
            Debug.LogWarning(
                "LevelStudyRecorder failed to send record:"
                + " url=" + url
                + ", error=" + request.error
                + ", responseCode=" + request.responseCode
            );
        }

        request.Dispose();
    }

    private string GetBackendUrl(string path)
    {
        string baseUrl = string.IsNullOrEmpty(backendBaseUrl)
            ? DefaultBackendBaseUrl
            : backendBaseUrl.TrimEnd('/');

        return baseUrl + path;
    }

    private void EnsureSessionId()
    {
        if (string.IsNullOrEmpty(sessionId))
        {
            sessionId = Guid.NewGuid().ToString("N");
        }
    }

    private string[] CloneRows(string[] rows)
    {
        if (rows == null)
        {
            return new string[0];
        }

        string[] copy = new string[rows.Length];

        for (int i = 0; i < rows.Length; i++)
        {
            copy[i] = rows[i] ?? "";
        }

        return copy;
    }

    private int GetMapWidth(string[] rows)
    {
        int width = 0;

        for (int i = 0; i < rows.Length; i++)
        {
            if (rows[i] != null)
            {
                width = Mathf.Max(width, rows[i].Length);
            }
        }

        return width;
    }

    private string GetMapHash(string[] rows)
    {
        unchecked
        {
            uint hash = 2166136261;

            for (int i = 0; i < rows.Length; i++)
            {
                string row = rows[i] ?? "";

                for (int j = 0; j < row.Length; j++)
                {
                    hash ^= row[j];
                    hash *= 16777619;
                }

                hash ^= '\n';
                hash *= 16777619;
            }

            return hash.ToString("x8");
        }
    }

    private int ManhattanDistance(Vector2Int first, Vector2Int second)
    {
        return Mathf.Abs(first.x - second.x) + Mathf.Abs(first.y - second.y);
    }

    private float RoundFloat(float value)
    {
        return Mathf.Round(value * 10000f) / 10000f;
    }
}

[Serializable]
public class LevelStartRecord
{
    public string eventType;
    public string sessionId;
    public string levelRunId;
    public int levelIndex;
    public string source;
    public string timestamp;
    public int solutionSteps;
    public int solverPushes;
    public int generationAttempts;
    public int reversePulls;
    public int searchedStates;
    public string[] rows;
    public LevelStructureData structure;
}

[Serializable]
public class LevelEndRecord
{
    public string eventType;
    public string sessionId;
    public string levelRunId;
    public int levelIndex;
    public bool completed;
    public string endReason;
    public float durationSeconds;
    public int moveCount;
    public int pushCount;
    public int restartCount;
    public string timestamp;
}

[Serializable]
public class LevelStructureData
{
    public string mapHash;
    public int width;
    public int height;
    public int boxCount;
    public int targetCount;
    public int wallCount;
    public int waterCount;
    public int emptyCount;
    public int floorCount;
    public float wallDensity;
    public float waterDensity;
    public float reachableAreaRatio;
    public float boxGoalDistanceAvg;
    public int boxGoalDistanceMin;
    public int deadCornerCount;
    public float deadCornerRisk;
}
