using System;
using UnityEngine;
using UnityEngine.UI;

public sealed class CoCreationEntryController : MonoBehaviour
{
    private const string DefaultCoCreationUrl =
        "http://111.231.136.4:8010/";

    private static readonly Color ReadyStatusColor =
        new Color(0.36f, 0.36f, 0.36f, 1f);
    private static readonly Color ErrorStatusColor =
        new Color(0.71f, 0.14f, 0.09f, 1f);

    [SerializeField]
    private string coCreationUrl = DefaultCoCreationUrl;

    [SerializeField]
    private Button openLabButton;

    [SerializeField]
    private Text statusText;

    private string validatedUrl;

    private void Awake()
    {
        if (openLabButton == null)
        {
            Debug.LogWarning(
                "CoCreationEntryController: Open lab button is missing."
            );
        }
        else
        {
            openLabButton.onClick.RemoveListener(OpenCoCreationLab);
            openLabButton.onClick.AddListener(OpenCoCreationLab);
        }

        ValidateConfiguration();
    }

    private void OnDestroy()
    {
        if (openLabButton != null)
        {
            openLabButton.onClick.RemoveListener(OpenCoCreationLab);
        }
    }

    public void OpenCoCreationLab()
    {
        if (!TryGetCoCreationUrl(out string targetUrl))
        {
            ApplyInvalidConfiguration();
            return;
        }

        validatedUrl = targetUrl;
        Debug.Log(
            "CoCreationEntryController: Opening independent co-creation lab: "
            + validatedUrl
        );
        Application.OpenURL(validatedUrl);
        SetStatus(
            "Open request sent. Unity does not track web progress yet.",
            ReadyStatusColor
        );
    }

    private void ValidateConfiguration()
    {
        if (!TryGetCoCreationUrl(out validatedUrl))
        {
            ApplyInvalidConfiguration();
            return;
        }

        if (openLabButton != null)
        {
            openLabButton.interactable = true;
        }

        SetStatus(
            "Ready to open the independent co-creation prototype.",
            ReadyStatusColor
        );
    }

    private bool TryGetCoCreationUrl(out string targetUrl)
    {
        targetUrl = null;
        string candidate = coCreationUrl?.Trim();

        if (string.IsNullOrEmpty(candidate)
            || !Uri.TryCreate(candidate, UriKind.Absolute, out Uri uri)
            || (uri.Scheme != Uri.UriSchemeHttp
                && uri.Scheme != Uri.UriSchemeHttps))
        {
            return false;
        }

        targetUrl = uri.AbsoluteUri;
        return true;
    }

    private void ApplyInvalidConfiguration()
    {
        validatedUrl = null;

        if (openLabButton != null)
        {
            openLabButton.interactable = false;
        }

        SetStatus(
            "The co-creation lab URL is missing or invalid.",
            ErrorStatusColor
        );
        Debug.LogWarning(
            "CoCreationEntryController: Co-creation URL must use HTTP or HTTPS."
        );
    }

    private void SetStatus(string message, Color color)
    {
        if (statusText == null)
        {
            return;
        }

        statusText.text = message;
        statusText.color = color;
    }
}
