using System;
using System.Collections;
using System.Collections.Generic;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;

public class OnlineMatchClient : MonoBehaviour
{
    public const string DefaultBackendBaseUrl = "http://111.231.136.4:8000";

    [SerializeField]
    private string backendBaseUrl = DefaultBackendBaseUrl;

    [SerializeField]
    private int requestTimeoutSeconds = 10;

    private readonly List<UnityWebRequest> activeRequests =
        new List<UnityWebRequest>();

    public IEnumerator CreateRoom(
        Action<OnlineRoomState> onSuccess,
        Action<string> onFailure)
    {
        return SendRoomRequest(
            UnityWebRequest.kHttpVerbPOST,
            "/online/rooms",
            "{}",
            false,
            onSuccess,
            onFailure
        );
    }

    public IEnumerator JoinRoom(
        string roomCode,
        Action<OnlineRoomState> onSuccess,
        Action<string> onFailure)
    {
        OnlineJoinPayload payload = new OnlineJoinPayload
        {
            roomCode = roomCode
        };
        return SendRoomRequest(
            UnityWebRequest.kHttpVerbPOST,
            "/online/rooms/join",
            JsonUtility.ToJson(payload),
            false,
            onSuccess,
            onFailure
        );
    }

    public IEnumerator GetRoom(
        Action<OnlineRoomState> onSuccess,
        Action<string> onFailure)
    {
        return SendRoomRequest(
            UnityWebRequest.kHttpVerbGET,
            BuildMatchPath(""),
            null,
            true,
            onSuccess,
            onFailure
        );
    }

    public IEnumerator SetReady(
        bool ready,
        Action<OnlineRoomState> onSuccess,
        Action<string> onFailure)
    {
        OnlineReadyPayload payload = new OnlineReadyPayload
        {
            ready = ready
        };
        return SendRoomRequest(
            UnityWebRequest.kHttpVerbPOST,
            BuildMatchPath("/ready"),
            JsonUtility.ToJson(payload),
            true,
            onSuccess,
            onFailure
        );
    }

    public IEnumerator LeaveRoom(
        Action<OnlineRoomState> onSuccess,
        Action<string> onFailure)
    {
        return SendRoomRequest(
            UnityWebRequest.kHttpVerbPOST,
            BuildMatchPath("/leave"),
            "{}",
            true,
            onSuccess,
            onFailure
        );
    }

    public IEnumerator SubmitChallenge(
        string[] rows,
        string aiAssistantMode,
        string opponentExperienceGoal,
        Action<OnlineRoomState> onSuccess,
        Action<string> onFailure)
    {
        OnlineChallengePayload payload = new OnlineChallengePayload
        {
            rows = CloneRows(rows),
            aiAssistantMode = aiAssistantMode,
            opponentExperienceGoal = opponentExperienceGoal
        };
        return SendRoomRequest(
            UnityWebRequest.kHttpVerbPOST,
            BuildMatchPath("/challenge"),
            JsonUtility.ToJson(payload),
            true,
            onSuccess,
            onFailure
        );
    }

    public IEnumerator SubmitResult(
        float durationSeconds,
        int moveCount,
        int minimumMoves,
        Action<OnlineRoomState> onSuccess,
        Action<string> onFailure)
    {
        OnlineResultPayload payload = new OnlineResultPayload
        {
            durationSeconds = durationSeconds,
            moveCount = moveCount,
            minimumMoves = minimumMoves
        };
        return SendRoomRequest(
            UnityWebRequest.kHttpVerbPOST,
            BuildMatchPath("/result"),
            JsonUtility.ToJson(payload),
            true,
            onSuccess,
            onFailure
        );
    }

    private IEnumerator SendRoomRequest(
        string method,
        string path,
        string jsonBody,
        bool requiresIdentity,
        Action<OnlineRoomState> onSuccess,
        Action<string> onFailure)
    {
        if (requiresIdentity && !OnlineMatchContext.HasMatch)
        {
            onFailure?.Invoke("No active online room.");
            yield break;
        }

        string url = backendBaseUrl.TrimEnd('/') + path;
        using (UnityWebRequest request = new UnityWebRequest(url, method))
        {
            request.downloadHandler = new DownloadHandlerBuffer();
            request.timeout = Mathf.Max(1, requestTimeoutSeconds);

            if (jsonBody != null)
            {
                byte[] body = Encoding.UTF8.GetBytes(jsonBody);
                request.uploadHandler = new UploadHandlerRaw(body);
                request.SetRequestHeader("Content-Type", "application/json");
            }

            if (requiresIdentity)
            {
                request.SetRequestHeader(
                    "X-Player-Token",
                    OnlineMatchContext.PlayerToken
                );
            }

            activeRequests.Add(request);
            yield return request.SendWebRequest();
            activeRequests.Remove(request);

            if (request.result != UnityWebRequest.Result.Success)
            {
                onFailure?.Invoke(BuildErrorMessage(request));
                yield break;
            }

            OnlineRoomState state = null;

            try
            {
                state = JsonUtility.FromJson<OnlineRoomState>(
                    request.downloadHandler.text
                );
            }
            catch (Exception exception)
            {
                Debug.LogWarning(
                    "OnlineMatchClient: Invalid room response: "
                    + exception.Message
                );
            }

            if (state == null || string.IsNullOrWhiteSpace(state.matchId))
            {
                onFailure?.Invoke("The server returned an invalid room response.");
                yield break;
            }

            onSuccess?.Invoke(state);
        }
    }

    private string BuildMatchPath(string suffix)
    {
        return "/online/rooms/"
            + Uri.EscapeDataString(OnlineMatchContext.MatchId)
            + suffix;
    }

    private static string BuildErrorMessage(UnityWebRequest request)
    {
        string responseText = request.downloadHandler != null
            ? request.downloadHandler.text
            : "";

        if (!string.IsNullOrWhiteSpace(responseText))
        {
            try
            {
                OnlineErrorResponse error =
                    JsonUtility.FromJson<OnlineErrorResponse>(responseText);

                if (error != null && !string.IsNullOrWhiteSpace(error.detail))
                {
                    return error.detail;
                }
            }
            catch (Exception)
            {
                // Fall through to the transport-level message.
            }
        }

        if (request.responseCode > 0)
        {
            return "Server request failed (HTTP " + request.responseCode + ").";
        }

        return string.IsNullOrWhiteSpace(request.error)
            ? "Unable to reach the online server."
            : request.error;
    }

    private static string[] CloneRows(string[] rows)
    {
        if (rows == null)
        {
            return null;
        }

        string[] clone = new string[rows.Length];

        for (int row = 0; row < rows.Length; row++)
        {
            clone[row] = rows[row] ?? "";
        }

        return clone;
    }

    private void OnDestroy()
    {
        for (int i = 0; i < activeRequests.Count; i++)
        {
            if (activeRequests[i] != null)
            {
                activeRequests[i].Abort();
            }
        }

        activeRequests.Clear();
    }

    [Serializable]
    private class OnlineJoinPayload
    {
        public string roomCode;
    }

    [Serializable]
    private class OnlineReadyPayload
    {
        public bool ready;
    }

    [Serializable]
    private class OnlineChallengePayload
    {
        public string[] rows;
        public string aiAssistantMode;
        public string opponentExperienceGoal;
    }

    [Serializable]
    private class OnlineResultPayload
    {
        public float durationSeconds;
        public int moveCount;
        public int minimumMoves;
    }

    [Serializable]
    private class OnlineErrorResponse
    {
        public string detail;
    }
}
