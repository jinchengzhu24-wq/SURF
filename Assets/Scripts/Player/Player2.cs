using UnityEngine;

public class Player2 : GridPlayerController
{
    protected override Vector3 ReadMoveDirection()
    {
        Vector3 dir = Vector3.zero;

        if (Input.GetKeyDown(KeyCode.UpArrow))
        {
            dir = Vector3.up;
        }

        if (Input.GetKeyDown(KeyCode.DownArrow))
        {
            dir = Vector3.down;
        }

        if (Input.GetKeyDown(KeyCode.LeftArrow))
        {
            dir = Vector3.left;
        }

        if (Input.GetKeyDown(KeyCode.RightArrow))
        {
            dir = Vector3.right;
        }

        return dir;
    }
}
