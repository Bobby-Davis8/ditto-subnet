// Command probe is a synthetic candidate for the rootless router integration
// test. It requests one route through host.docker.internal (the in-namespace
// router) and one through a host-namespace address with the same Host header,
// then prints the two HTTP status codes as JSON. It carries no credentials.
package main

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"os"
	"time"
)

func status(target, dial string, deadline time.Duration, want func(int) bool) int {
	transport := &http.Transport{Proxy: nil, DisableKeepAlives: true}
	if dial != "" {
		transport.DialContext = func(ctx context.Context, network, _ string) (net.Conn, error) {
			return (&net.Dialer{Timeout: 2 * time.Second}).DialContext(ctx, network, dial)
		}
	}
	client := &http.Client{Transport: transport, Timeout: 5 * time.Second}
	last := 0
	for stop := time.Now().Add(deadline); time.Now().Before(stop); time.Sleep(250 * time.Millisecond) {
		response, err := client.Post(target, "application/json", nil)
		if err != nil {
			continue
		}
		_ = response.Body.Close()
		last = response.StatusCode
		if want(last) {
			break
		}
	}
	return last
}

func main() {
	if len(os.Args) != 4 {
		os.Exit(2)
	}
	result := map[string]int{
		// Retry until the test has registered this container's source address.
		"in_namespace":   status(os.Args[1], "", 90*time.Second, func(code int) bool { return code == http.StatusOK }),
		"host_namespace": status(os.Args[2], os.Args[3], 20*time.Second, func(code int) bool { return code != 0 }),
	}
	if json.NewEncoder(os.Stdout).Encode(result) != nil {
		os.Exit(1)
	}
}
