# from pynq import Overlay
# from pynq.ps import Clocks
# import numpy as np
# import time

# # Load the overlay
# ol = Overlay("/home/xilinx/jupyter_notebooks/Final_project_test/design_1_wrapper.bit")
# bram = ol.axi_bram_ctrl_0

# # Initialize the GPIO for the buttons
# buttons = ol.axi_gpio_0.channel1 

# # Initial Map Data
# data = [
#     0xFFFFFFFF, # 11111111111111111111111111111111 (Top Border)
#     0x80000001, # 10000000000000000000000000000001
#     0xBF0F0F03, # 10111111000011110000111100000011 (Room dividers)
#     0x81010101, # 10000001000000010000000100000001
#     0xBF0F0F03, # 10111111000011110000111100000011
#     0x80000001, # 10000000000000000000000000000001
#     0x80FF0001, # 10000000111111110000000000000001
#     0x80810001, # 10000000100000010000000000000001
#     0x80810FF1, # 10000000100000010000111111110001
#     0x80010801, # 10000000000000010000100000000001
#     0x80000801, # 10000000000000000000100000000001 <- (Player spawns near here)
#     0x8FFFF801, # 10001111111111111111100000000001
#     0x80000801, # 10000000000000000000100000000001
#     0x80000801, # 10000000000000000000100000000001
#     0x83C00FFF, # 10000011110000000000111111111111 (Long wall)
#     0x82400801, # 10000010010000000000100000000001
#     0x82400801, # 10000010010000000000100000000001
#     0x824FF801, # 10000010010011111111100000000001
#     0x82400001, # 10000010010000000000000000000001
#     0x83FFFF01, # 10000011111111111111111100000001
#     0x80000001, # 10000000000000000000000000000001
#     0xFFC003FF, # 11111111110000000000001111111111 (Bottleneck gate)
#     0x80400201, # 10000000010000000000001000000001
#     0x80400201, # 10000000010000000000001000000001
#     0x80400201, # 10000000010000000000001000000001
#     0xFF4002FF, # 11111111010000000000001011111111
#     0x80000001, # 10000000000000000000000000000001
#     0x8AAAAAA1, # 10001010101010101010101010100001 (Pillars!)
#     0x80000001, # 10000000000000000000000000000001
#     0x80000001, # 10000000000000000000000000000001
#     0x80000001, # 10000000000000000000000000000001
#     0xFFFFFFFF, # 11111111111111111111111111111111 (Bottom Border)
#     0x28002800, # Initial X and Y (10.0, 10.0 in fixed point)
#     0x00000000, # Initial Angle
# ]

# # Write initial static data to BRAM
# for i, val in enumerate(data):
#     bram.write(i * 4, val)
    
# # Set correct clock frequency
# Clocks.fclk0_mhz = 50.0
# time.sleep(1) 

# # --- Player State Variables ---
# x_val = 10.0
# y_val = 10.0
# angle_int = 0    # Natively tracked as an integer from 0 to 4095

# # --- Tuning Parameters ---
# move_speed = 0.2
# turn_step = 64   # How many 12-bit units to turn per tick (4096 / 64 = 64 steps for a full circle)

# print("Ready! Use buttons on the PYNQ board:")
# print("BTN 3: Turn Left | BTN 2: Move Forward | BTN 1: Move Backward | BTN 0: Turn Right")

# try:
#     while True:
#         btn_state = buttons.read()
        
#         btn_0 = (btn_state & 0x1) > 0 # Turn Right
#         btn_1 = (btn_state & 0x2) > 0 # Move Backward
#         btn_2 = (btn_state & 0x4) > 0 # Move Forward
#         btn_3 = (btn_state & 0x8) > 0 # Turn Left
        
#         # --- Update Angle (Native 12-bit) ---
#         if btn_3:
#             angle_int = (angle_int + turn_step) % 4096
#         if btn_0:
#             angle_int = (angle_int - turn_step) % 4096
            
#         # --- Update Position ---
#         # Convert 0-4095 to Radians strictly for Python's movement math
#         theta_rad = angle_int * (2 * np.pi / 4096)
        
#         if btn_2: # Forward
#             x_val += move_speed * np.cos(theta_rad)
#             y_val += move_speed * np.sin(theta_rad)
#         if btn_1: # Backward 
#             x_val -= move_speed * np.cos(theta_rad)
#             y_val -= move_speed * np.sin(theta_rad)

#         # --- Write to BRAM ---
#         print(f"x coord: {x_val}, y coord: {y_val}, angle: {angle_int}")
#         # 1. Coordinates (6.10 fixed point)
#         x_fixed = int(x_val * 1024) & 0xFFFF
#         y_fixed = int(y_val * 1024) & 0xFFFF
#         coord_word = (x_fixed << 16) | y_fixed
#         bram.write(32 * 4, coord_word)
        
#         # 2. Angle (Directly write the 12-bit integer)
#         bram.write(33 * 4, angle_int)
        
#         # Loop delay
#         time.sleep(0.05) 
        
# except KeyboardInterrupt:
#     print("\nProgram stopped.")

# from pynq import Overlay
# from pynq.ps import Clocks
# import numpy as np
# import time
# import socket
# import protocol_1 as protocol

# # --- Network Configuration ---
# SERVER_IP = "3.9.71.204"
# SERVER_PORT = 9000
# TICK_RATE = 20  
# TICK_INTERVAL = 1.0 / TICK_RATE

# # --- Hardware Setup ---
# ol = Overlay("/home/xilinx/jupyter_notebooks/Final_project_test/design_1_wrapper.bit")
# bram = ol.axi_bram_ctrl_0
# buttons = ol.axi_gpio_0.channel1 

# # Map Data (Your Maze)
# data = [
#     0xFFFFFFFF, 0x80000001, 0xBF0F0F03, 0x81010101, 0xBF0F0F03, 0x80000001,
#     0x80FF0001, 0x80810001, 0x80810FF1, 0x80010801, 0x80000801, 0x8FFFF801,
#     0x80000801, 0x80000801, 0x83C00FFF, 0x82400801, 0x82400801, 0x824FF801,
#     0x82400001, 0x83FFFF01, 0x80000001, 0xFFC003FF, 0x80400201, 0x80400201,
#     0x80400201, 0xFF4002FF, 0x80000001, 0x8AAAAAA1, 0x80000001, 0x80000001,
#     0x80000001, 0xFFFFFFFF, 0x28002800, 0x00000000
# ]

# for i, val in enumerate(data):
#     bram.write(i * 4, val)
    
# Clocks.fclk0_mhz = 60.0
# time.sleep(1) 

# # --- Player State Variables ---
# x_val = 10.0
# y_val = 10.0
# angle_int = 0    
# move_speed = 0.2
# turn_step = 64   

# # --- Network Registration Phase ---
# print(f"Connecting to AWS Server at {SERVER_IP}:{SERVER_PORT}...")
# sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
# server_address = (SERVER_IP, SERVER_PORT)
# seq_num = 0

# # Send first contact
# reg_packet = protocol.pack_register_packet(seq=seq_num, x=x_val, y=y_val, angle=0.0)
# sock.sendto(reg_packet, server_address)
# seq_num += 1

# sock.settimeout(5.0)
# registered = False
# while not registered:
#     try:
#         rx_data, _ = sock.recvfrom(1024)
#         rx_type, rx_seq, rx_ts, *rest = protocol.unpack_server_packet(rx_data)
#         if rx_type in [protocol.PKT_ACK, protocol.PKT_GAME_STATE]:
#             print("[SERVER] Connection established!")
#             registered = True
#     except socket.timeout:
#         print("No response, retrying registration...")
#         sock.sendto(reg_packet, server_address)

# # Set socket to non-blocking for the fast game loop
# sock.setblocking(False)

# print("Ready! Use buttons to move. Broadcasting state to AWS.")
# try:
#     while True:
#         loop_start = time.time()

#         # --- 1. Read Inputs & Update State ---
#         btn_state = buttons.read()
#         btn_0 = (btn_state & 0x1) > 0 # Turn Right
#         btn_1 = (btn_state & 0x2) > 0 # Move Backward
#         btn_2 = (btn_state & 0x4) > 0 # Move Forward
#         btn_3 = (btn_state & 0x8) > 0 # Turn Left
        
#         if btn_3:
#             angle_int = (angle_int + turn_step) % 4096
#         if btn_0:
#             angle_int = (angle_int - turn_step) % 4096
            
#         theta_rad = angle_int * (2 * np.pi / 4096)
        
#         if btn_2: 
#             x_val += move_speed * np.cos(theta_rad)
#             y_val += move_speed * np.sin(theta_rad)
#         if btn_1: 
#             x_val -= move_speed * np.cos(theta_rad)
#             y_val -= move_speed * np.sin(theta_rad)

#         # --- 2. Update Hardware (BRAM) ---
#         x_fixed = int(x_val * 1024) & 0xFFFF
#         y_fixed = int(y_val * 1024) & 0xFFFF
#         coord_word = (x_fixed << 16) | y_fixed
#         bram.write(32 * 4, coord_word)
#         bram.write(33 * 4, angle_int)

#         # --- 3. Transmit State to Server ---
#         packet = protocol.pack_node_packet(
#             pkt_type=protocol.PKT_STATE_UPDATE,
#             seq=seq_num,
#             x=x_val, y=y_val, angle=theta_rad, flags=0
#         )
#         sock.sendto(packet, server_address)
#         seq_num = (seq_num + 1) & 0xFFFF

#         # --- 4. Drain Incoming Packets ---
#         # This keeps the buffer clean and is where we receive opponent data
#         while True:
#             try:
#                 rx_data, _ = sock.recvfrom(1024)
#                 # Later, we will unpack this to find where the other player is!
#             except BlockingIOError:
#                 # No more packets waiting, break out of drain loop
#                 break

#         # --- 5. Maintain Tick Rate ---
#         # Instead of a hard 0.05 sleep, we subtract the time it took to do the math/networking
#         elapsed = time.time() - loop_start
#         sleep_time = TICK_INTERVAL - elapsed
#         if sleep_time > 0:
#             time.sleep(sleep_time) 
        
# except KeyboardInterrupt:
#     print("\nProgram stopped.")
#     sock.close()