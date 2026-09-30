/*
 * ViewSpector — payload core module.
 *
 * Limits the payload enforces on the wire, in one place so the socket server, the
 * framing and every tree builder agree on them.
 */
package com.oberkfell.viewspector.agent.payload

object WireLimits {

    /**
     * Deepest tree the payload sends: a node at this depth (the root is depth 1) keeps its
     * own fields but none of its children, and is flagged (ViewNode.Flag.CHILDREN_TRUNCATED,
     * A11yNode.children_truncated) with a "depth-truncated=N" diagnostics token.
     *
     * Protobuf parsers stop at about 100 nested messages (upb and the pure-Python runtime
     * both default to 100) and then reject the WHOLE message, so one very deep branch
     * would make an entire dump unreadable on the host. Measured with the host's upb
     * runtime: a Response carrying A11yNode or ComposeNode trees 97 nodes deep fails
     * to parse (ViewNode: 98). Every tree sits under 2-3 envelope messages and carries
     * Bounds > Rect/Quad below each node, so 80 levels leaves a margin of about 15.
     * host/tests/test_wire_depth.py reads this constant and checks that margin.
     */
    const val MAX_TREE_DEPTH = 80

    /**
     * Largest request frame the payload accepts. Real requests are well under 1 KiB;
     * a larger LEN is a corrupt or hostile stream, so the connection is dropped before
     * anything is allocated for it.
     */
    const val MAX_REQUEST_BYTES = 16 * 1024 * 1024

    /**
     * Client connections served at once (the MCP server holds one; CLI calls open and
     * close one each). A connection over the cap gets an ERROR reply and is closed.
     */
    const val MAX_CONNECTIONS = 8
}
