import asyncio
import json
import datetime
import logging
import os
import socket
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from signal import SIGINT, signal

import RPi.GPIO as gpio
import websockets

from remla.settings import *


class NoDeviceError(Exception):
    def __init__(self, device_name):
        self.device_name = device_name

    def __str__(self):
        return "NoDeviceError: This experiment doesn't have a device, '{0}'".format(
            self.device_name
        )


def runMethod(device, method, params):
    if hasattr(device, "cmdHandler"):
        func = getattr(device, "cmdHandler")
        try:
            result = func(method, params, device.name)
            return result
        except Exception as e:
            logging.exception(f"Exception while running {device.name}.{method} params={params}: {e}")
            raise
    else:
        logging.error(f"Device {device} does not have a cmdHandler method")
        raise


class Experiment(object):
    def __init__(self, name, host="localhost", port=8675, admin=False):
        self.name = name
        self.host = host
        self.port = port
        self.devices = {}

        self.lockGroups = {}
        self.lockMapping = {}

        self.allStates = {}
        self.clients = deque()
        self.activeClient = None

        self.initializedStates = False
        self.admin = admin
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.logPath = logsDirectory / f"{self.name}.log"
        # self.jsonFile = os.path.join(self.directory, self.name + ".json")
        logging.basicConfig(
            filename=self.logPath,
            level=logging.DEBUG,
            format="%(levelname)s - %(asctime)s - %(filename)s - %(funcName)s \r\n %(message)s \r\n",
        )
        logging.info("""
        ##############################################################
        ####                Starting New Log                      ####
        ##############################################################    
        """)
        self.startIpcListener()
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def logException(self, task):
        if task.exception():
            logging.exception("Unknown Exception: %s", task.exception())

    def addDevice(self, device):
        device.experiment = self
        logging.info("Adding Device - " + device.name)
        self.devices[device.name] = device

    def addLockGroup(self, name: str, devices):
        lock = asyncio.Lock()
        self.lockGroups[name] = lock
        for device in devices:
            self.lockMapping[device.name] = name

    def recallState(self):
        logging.info("Recalling State")
        with open(self.jsonFile, "r") as f:
            self.allStates = json.load(f)
        for name, device in self.devices.items():
            device.setState(self.allStates[name])
        self.initializedStates = True

    def getControllerStates(self):
        logging.info("Getting Controller States")
        for name, device in self.devices.items():
            self.allStates[name] = device.getState()
        with open(self.jsonFile, "w") as f:
            json.dump(self.allStates, f)
        self.initializedStates = True

    async def handleConnection(self, websocket, path):
    logging.info(f"New websocket connection: {websocket} path={path}")
    logging.debug(f"Connection! {websocket} {path}")
    self.clients.append(websocket)  # Track all clients by their WebSocket
        try:
            if self.activeClient is None and self.clients:
                self.activeClient = websocket
                logging.info(f"Assigned active client: {websocket}")
                await self.sendAlert(
                    websocket, "Experiment/controlStatus/1,You have control of the lab equipment."
                )
            else:
                logging.info(f"Connected client without control: {websocket}")
                await self.sendAlert(
                    websocket,
                    "Experiment/controlStatus/0,You are connected but do not have control of the lab equipment.",
                )
            async for command in websocket:
                if websocket == self.activeClient:
                    logging.debug(f"Received command from active client: {command}")
                    task = asyncio.create_task(self.processCommand(command, websocket))
                    task.add_done_callback(self.logException)
                else:
                    logging.warning(f"Client {websocket} tried to send command but is not active")
                    asyncio.create_task(
                        self.sendAlert(
                            websocket, "Experiment/controlStatus/0,You do not have control to send commands."
                        )
                    )
        finally:
            logging.info(f"Client disconnected: {websocket}")
            self.clients.remove(websocket)  # Remove client that closed connection
            if (
                websocket == self.activeClient
            ):  # if the removed client was the active client
                self.activeClient = (
                    self.clients[0] if len(self.clients) > 0 else None
                )  # set the first client in the list to be the new active client
                if self.activeClient is not None:
                    logging.info(f"New active client assigned: {self.activeClient}")
                    await self.sendAlert(
                        self.activeClient, "Experiment/controlStatus/1,You are the new active client."
                    )
                logging.debug("the first client has changed!")
                self.resetExperiment()
                # logging.info("Looping through devices - resetting them.")
                # for deviceName, device in self.devices.items():
                #     logging.info("Running reset and cleanup on device " + deviceName)
                #     device.reset()
                # logging.info("Everything reset properly!")

    async def processCommand(self, command, websocket):
    logging.debug(f"Processing Command {command} from {websocket}")
    logging.info("Processing Command - " + command)
        # Parse either JSON envelope or legacy slash-format
        try:
            deviceName, cmd, params, req_id = self._parse_incoming(command)
            logging.debug(f"Parsed command -> device={deviceName} cmd={cmd} params={params} id={req_id}")
        except Exception as e:
            logging.error(f"Failed to parse incoming command: {command} error={e}")
            raise
        if deviceName not in self.devices:
            logging.error(f"Raising NoDeviceError for {deviceName}")
            raise NoDeviceError(deviceName)

        # Execute command and capture controller response
    logging.info(f"Running device method: {deviceName}.{cmd} params={params}")
        try:
            response = await self.runDeviceMethod(deviceName, cmd, params, websocket)
            logging.debug(f"Raw controller response: {response}")
        except Exception as e:
            logging.exception(f"Device method {deviceName}.{cmd} raised exception: {e}")
            await self.send_structured(websocket, "error", "Experiment", topic=f"{deviceName}/{cmd}", payload={"error": str(e)}, msg_id=req_id)
            return

        # If controller returned a value, wrap and send structured response
        if response is not None:
            kind, topic, payload, _ = self._wrap_controller_response(response, deviceName, req_id)
            logging.info(f"Sending structured response to client: kind={kind} topic={topic} payload={payload} id={req_id}")
            await self.send_structured(websocket, kind, deviceName, topic, payload, msg_id=req_id)

    async def runDeviceMethod(self, deviceName, method, params, websocket):
        device = self.devices.get(deviceName)

        lockGroupName = self.lockMapping.get(deviceName)
        if lockGroupName:
            async with self.lockGroups[lockGroupName]:
                loop = asyncio.get_event_loop()
                logging.debug(f"Acquired lock for group {lockGroupName} to run {deviceName}.{method}")
                response = await loop.run_in_executor(
                    self.executor, runMethod, device, method, params
                )
                logging.debug(f"Device method completed {deviceName}.{method} -> {response}")
                # Return the raw controller response to be normalized by the caller
                return response
        else:
            logging.error("All devices need a lock")
            raise
            # result = await self.runMethod(device, method, params)
        # unreachable; runDeviceMethod returns the controller's response when locked

    def startServer(self):
        # This function sets up and runs the WebSocket server indefinitely
        # loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
    start_server = websockets.serve(self.handleConnection, self.host, self.port)

    logging.info(f"Server started at ws://{self.host}:{self.port}")
    self.loop.run_until_complete(start_server)
        self.loop.run_forever()

    async def sendDataToClient(self, websocket, dataStr: str):
        try:
            logging.debug(f"Sending raw data to client {websocket}: {dataStr}")
            await websocket.send(dataStr)
        except websockets.exceptions.ConnectionClosed:
            logging.warning(
                f"Failed to send message: {dataStr} - Connection was closed."
            )
            print(f"Failed to send message: {dataStr} - Connection was closed.")

    async def send_structured(
        self, websocket, kind: str, source: str, topic: str = None, payload=None, msg_id=None, meta: dict = None
    ):
        """
        Unified JSON envelope for messages sent to clients.
        kind: 'message'|'alert'|'response'|'error'|'event'|'command'
        source: origin (device name or 'Experiment')
        topic: optional routing string (e.g. 'device/position')
        payload: dict or string
        msg_id: correlates to client request id
        """
        envelope = {
            "type": kind,
            "id": msg_id,
            "source": source,
            "topic": topic,
            "payload": payload,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "meta": meta or {},
        }
        logging.info(f"Sending structured message to {websocket}: type={kind} source={source} topic={topic} id={msg_id}")
        await self.sendDataToClient(websocket, json.dumps(envelope))

    async def sendMessage(self, websocket, message: str, msg_id: str = None, source: str = "Experiment"):
        await self.send_structured(websocket, "message", source, payload=message, msg_id=msg_id)

    async def sendAlert(self, websocket, alertMsg: str, msg_id: str = None, source: str = "Experiment"):
        await self.send_structured(websocket, "alert", source, payload=alertMsg, msg_id=msg_id)

    async def sendCommandToClient(self, websocket, command: str, msg_id: str = None, source: str = "Experiment"):
        await self.send_structured(websocket, "command", source, payload=command, msg_id=msg_id)

    def _parse_incoming(self, raw: str):
        """
        Accept either legacy 'Device/command/arg1,arg2' or the JSON command envelope.
        Returns tuple (device, action, params_list, id)
        """
        raw = raw.strip()
        # try JSON first
        try:
            obj = json.loads(raw)
            if obj.get("type") == "command":
                return obj.get("device"), obj.get("action"), obj.get("params", []), obj.get("id")
        except Exception:
            pass
        # fallback legacy format: Device/action/arg1,arg2
        try:
            deviceName, cmd, params = raw.split("/", 2)
            params_list = params.split(",") if params else []
            return deviceName, cmd, params_list, None
        except Exception:
            raise ValueError("Invalid command format")

    def _wrap_controller_response(self, response, device_name, request_id=None):
        """
        Normalize controller return values into (kind, topic, payload, request_id).
        controller may return: ("ALERT", "Device/position/limit"), ("MESSAGE", "..."), a bare string, or a dict.
        """
        # Default
        kind = "message"
        topic = None
        payload = None

        # If controller returned a tuple/list
        if isinstance(response, (tuple, list)):
            if len(response) >= 2 and isinstance(response[0], str):
                rtype = response[0]
                content = response[1]
            elif len(response) == 1:
                rtype = "MESSAGE"
                content = response[0]
            else:
                rtype = "MESSAGE"
                content = response
        elif isinstance(response, str):
            rtype = "MESSAGE"
            content = response
        elif isinstance(response, dict):
            rtype = "MESSAGE"
            content = response
        else:
            rtype = "MESSAGE"
            content = str(response)

        kind_map = {"ALERT": "alert", "MESSAGE": "message", "ERROR": "error"}
        kind = kind_map.get(rtype, "message")

        # Try to extract topic/payload from slash-format strings
        if isinstance(content, str) and "/" in content:
            parts = content.split("/", 2)
            if len(parts) == 3:
                topic = "/".join(parts[:2])
                payload = parts[2]
            else:
                topic = parts[0]
                payload = "/".join(parts[1:])
        else:
            payload = content

        return kind, topic or device_name, payload, request_id

    def deviceNames(self):
        names = []
        for deviceName in self.devices:
            names.append(deviceName)
        return names

    async def onClientDisconnect(self, websocket):
        # Remove client from the client queue if they disconnect
        if websocket in self.clients:
            self.clients.remove(websocket)
        if websocket == self.activeClient:
            self.activeClient = None
            # Pass control to the next available client in the queue
            while self.clientQueue:
                potentialController = self.clientQueue.popleft()
                if potentialController.open:
                    self.activeClient = potentialController
                    await self.sendMessage(
                        self.activeClient, "You now have control of the lab equipment."
                    )
                    break
            if not self.activeClient:
                print("No active clients")
                logging.info("No active clients")
                self.activeClient = None

            logging.info(f"Active client disconnected: {websocket}.")
        else:
            logging.info(f"Non-active client disconnected: {websocket}.")

    def exitHandler(self, signalReceived, frame):
        logging.info("Attempting to exit")
        if self.socket is not None:
            self.socket.close()
            logging.info("Socket is closed")

        # if self.messengerSocket is not None:
        #     self.messengerSocket.close()
        #     logging.info("Messenger socket closed")

        if not self.admin:
            self.resetExperiment()
        else:
            gpio.cleanup()
        exit(0)

    def setupSignalHandlers(self):
        signal.signal(signal.SIGINT, self.exitHandler)
        signal.signal(signal.SIGTERM, self.exitHandler)

    def closeHandler(self):
        logging.info("Client Disconnected. Handling Close.")
        if self.connection is not None:
            self.connection.close()
            logging.info("Connection to client closed.")
        if not self.admin:
            for deviceName, device in self.devices.items():
                logging.info("Running reset on device " + deviceName)
                device.reset()

    def setup(self):
        try:
            if not self.initializedStates:
                self.getControllerStates()
            if not os.path.exists(self.socketPath):
                f = open(self.socketPath, "w")
                f.close()

            # if self.messenger is not None:
            #     self.messengerThread = threading.Thread(
            #         target=self.messenger.setup, daemon=True
            #     )
            #     self.messengerThread.start()
            os.unlink(self.socketPath)
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            signal(SIGINT, self.exitHandler)
            signal(SIGTERM, self.exitHandler)
            self.socket.bind(self.socketPath)
            self.socket.listen(1)
            self.socket.setTimeout(1)
            self.__waitToConnect()
        except OSError:
            if os.path.exists(self.socketPath):
                print(
                    f"Error accessing {self.socketPath}\nTry running 'sudo chown pi: {self.socketPath}'"
                )
                os._exit(0)
                return
            else:
                print(
                    f"Socket file not found. Did you configure uv4l-uvc.conf to use {self.socketPath}?"
                )
                raise
            logging.error("Socket Error!", exc_info=True)
            print(f"Socket error: {err}")

    def startIpcListener(self, ipc_path="/tmp/remla_cmd.sock"):
        # Remove old socket if exists
        if os.path.exists(ipc_path):
            os.unlink(ipc_path)
        ipc_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ipc_sock.bind(ipc_path)
    ipc_sock.listen(1)
    logging.info(f"IPC listener started at {ipc_path}")

        def ipc_loop():
            while True:
                conn, _ = ipc_sock.accept()
                data = conn.recv(1024).decode().strip()
                if data in ["boot", "contact"]:
                    # Send message to active client as structured JSON
                    logging.info(f"IPC received event '{data}'")
                    if self.activeClient:
                        future = asyncio.run_coroutine_threadsafe(
                            self.send_structured(self.activeClient, "event", "Experiment", topic=f"message/{data}", payload={"msg": data}),
                            self.loop,
                        )
                        logging.debug(f"Sent {data} message to active client.")
                    else:
                        logging.info(f"No active client to send IPC event '{data}'")
                        logging.debug(f"No active client to send {data} message.")
                conn.close()


        threading.Thread(target=ipc_loop, daemon=True).start()

    def resetExperiment(self):
        logging.info("Resetting experiment to original state.")
        for deviceName, device in self.devices.items():
            logging.info(f"Resetting device {deviceName}")
            device.reset()
        logging.info("Experiment reset complete.")