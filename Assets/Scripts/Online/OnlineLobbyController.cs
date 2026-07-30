using System.Collections;
using System.Text;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class OnlineLobbyController : MonoBehaviour
{
    private const string BriefingSceneName = "Match_Briefing";
    private const string MenuSceneName = "Menu";

    private OnlineMatchClient client;
    private Button createButton;
    private Button joinButton;
    private Button backButton;
    private InputField roomCodeInput;
    private Text generatedRoomCodeText;
    private Text footerHint;
    private Coroutine pollingCoroutine;
    private bool busy;
    private bool leavingScene;

    private void Start()
    {
        OnlineSceneUi.EnsureEventSystem();
        client = gameObject.AddComponent<OnlineMatchClient>();
        ResolveUi();
        WireUi();

        if (OnlineMatchContext.HasMatch)
        {
            ShowGeneratedCode(OnlineMatchContext.RoomCode);
            SetStatus("Waiting for your opponent to join...");
            SetRoomActionsInteractable(false);
            StartPolling();
        }
        else
        {
            SetStatus("Create a room or enter a 6-digit room code.");
            SetRoomActionsInteractable(true);
        }
    }

    private void ResolveUi()
    {
        createButton = OnlineSceneUi.EnsureButton("CreateRoomButton");
        joinButton = OnlineSceneUi.EnsureButton("JoinRoomButton");
        backButton = OnlineSceneUi.EnsureButton("BackButton");
        generatedRoomCodeText =
            OnlineSceneUi.FindText("GeneratedRoomCodeFieldText");
        footerHint = OnlineSceneUi.FindText("FooterHint");

        GameObject fieldObject = GameObject.Find("RoomCodeField");
        Text fieldText = OnlineSceneUi.FindText("RoomCodeFieldText");

        if (fieldObject != null && fieldText != null)
        {
            roomCodeInput = fieldObject.GetComponent<InputField>();

            if (roomCodeInput == null)
            {
                roomCodeInput = fieldObject.AddComponent<InputField>();
            }

            roomCodeInput.targetGraphic = fieldObject.GetComponent<Graphic>();
            roomCodeInput.textComponent = fieldText;
            roomCodeInput.characterLimit = 6;
            roomCodeInput.contentType = InputField.ContentType.Alphanumeric;
            roomCodeInput.lineType = InputField.LineType.SingleLine;
            roomCodeInput.SetTextWithoutNotify("");
        }
    }

    private void WireUi()
    {
        if (createButton != null)
        {
            createButton.onClick.AddListener(CreateRoom);
        }

        if (joinButton != null)
        {
            joinButton.onClick.AddListener(JoinRoom);
        }

        if (backButton != null)
        {
            backButton.onClick.AddListener(Back);
        }

        if (roomCodeInput != null)
        {
            roomCodeInput.onValueChanged.AddListener(NormalizeRoomCodeInput);
        }
    }

    private void CreateRoom()
    {
        if (busy || OnlineMatchContext.HasMatch)
        {
            return;
        }

        busy = true;
        SetRoomActionsInteractable(false);
        SetStatus("Creating room...");
        StartCoroutine(
            client.CreateRoom(
                state =>
                {
                    try
                    {
                        OnlineMatchContext.Initialize(state);
                    }
                    catch (System.ArgumentException exception)
                    {
                        HandleActionFailure(exception.Message);
                        return;
                    }

                    busy = false;
                    ShowGeneratedCode(state.roomCode);
                    SetStatus("Share the room code. Waiting for your opponent...");
                    StartPolling();
                },
                HandleActionFailure
            )
        );
    }

    private void JoinRoom()
    {
        if (busy || OnlineMatchContext.HasMatch)
        {
            return;
        }

        string roomCode = NormalizeRoomCode(
            roomCodeInput != null ? roomCodeInput.text : ""
        );

        if (roomCode.Length != 6)
        {
            SetStatus("Enter a valid 6-digit room code.");
            return;
        }

        busy = true;
        SetRoomActionsInteractable(false);
        SetStatus("Joining room...");
        StartCoroutine(
            client.JoinRoom(
                roomCode,
                state =>
                {
                    try
                    {
                        OnlineMatchContext.Initialize(state);
                    }
                    catch (System.ArgumentException exception)
                    {
                        HandleActionFailure(exception.Message);
                        return;
                    }

                    LoadBriefing();
                },
                HandleActionFailure
            )
        );
    }

    private void StartPolling()
    {
        if (pollingCoroutine == null)
        {
            pollingCoroutine = StartCoroutine(PollRoom());
        }
    }

    private IEnumerator PollRoom()
    {
        while (!leavingScene && OnlineMatchContext.HasMatch)
        {
            bool completed = false;

            yield return client.GetRoom(
                state =>
                {
                    OnlineMatchContext.ApplyState(state);
                    completed = true;

                    if (state.status == "briefing"
                        && state.players != null
                        && state.players.Length >= 2)
                    {
                        LoadBriefing();
                    }
                    else if (state.status == "cancelled")
                    {
                        OnlineMatchContext.Clear();
                        SetStatus("The room was cancelled.");
                        SetRoomActionsInteractable(true);
                    }
                },
                error =>
                {
                    completed = true;
                    SetStatus("Connection error: " + error);
                }
            );

            if (leavingScene || !OnlineMatchContext.HasMatch)
            {
                break;
            }

            if (!completed)
            {
                SetStatus("Unable to update room status.");
            }

            yield return new WaitForSecondsRealtime(1f);
        }

        pollingCoroutine = null;
    }

    private void Back()
    {
        if (leavingScene)
        {
            return;
        }

        leavingScene = true;
        SetRoomActionsInteractable(false);

        if (!OnlineMatchContext.HasMatch)
        {
            OnlineMatchContext.Clear();
            SceneManager.LoadScene(MenuSceneName);
            return;
        }

        SetStatus("Leaving room...");
        StartCoroutine(
            client.LeaveRoom(
                state => FinishBackNavigation(),
                error => FinishBackNavigation()
            )
        );
    }

    private void FinishBackNavigation()
    {
        OnlineMatchContext.Clear();
        SceneManager.LoadScene(MenuSceneName);
    }

    private void LoadBriefing()
    {
        if (leavingScene)
        {
            return;
        }

        leavingScene = true;
        SceneManager.LoadScene(BriefingSceneName);
    }

    private void HandleActionFailure(string error)
    {
        busy = false;
        SetRoomActionsInteractable(true);
        SetStatus("Online request failed: " + error);
    }

    private void NormalizeRoomCodeInput(string value)
    {
        string normalized = NormalizeRoomCode(value);

        if (roomCodeInput != null && roomCodeInput.text != normalized)
        {
            roomCodeInput.SetTextWithoutNotify(normalized);
        }
    }

    private static string NormalizeRoomCode(string value)
    {
        StringBuilder normalized = new StringBuilder(6);
        string uppercase = (value ?? "").Trim().ToUpperInvariant();

        for (int i = 0; i < uppercase.Length && normalized.Length < 6; i++)
        {
            char character = uppercase[i];

            if ((character >= 'A' && character <= 'Z')
                || (character >= '0' && character <= '9'))
            {
                normalized.Append(character);
            }
        }

        return normalized.ToString();
    }

    private void ShowGeneratedCode(string roomCode)
    {
        if (generatedRoomCodeText != null)
        {
            generatedRoomCodeText.text = string.IsNullOrWhiteSpace(roomCode)
                ? "ROOM CODE APPEARS HERE"
                : roomCode;
        }
    }

    private void SetStatus(string message)
    {
        if (footerHint != null)
        {
            footerHint.text = message;
        }
    }

    private void SetRoomActionsInteractable(bool interactable)
    {
        if (createButton != null)
        {
            createButton.interactable = interactable;
        }

        if (joinButton != null)
        {
            joinButton.interactable = interactable;
        }

        if (roomCodeInput != null)
        {
            roomCodeInput.interactable = interactable;
        }
    }
}
